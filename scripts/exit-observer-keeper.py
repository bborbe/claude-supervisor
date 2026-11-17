#!/usr/bin/env python3
"""Watch every manager pane's exit writer, and re-arm the ones that have died.

**The shape is one long-lived process, not a tick.** A sweep costs ~24 s (measured 2026-11-17
on an idle host), so re-sweeping on a timer spends ~40 % of a core and — worse — cannot hold a
sub-minute detection bound: the moment one sweep runs long, detection is one sweep plus a tick.
So this process sweeps ONCE at start, refreshes the pane list on a slow interval, and between
refreshes only `stat`s each watched log. A log is appended on every heartbeat, so its mtime is
the writer's last sign of life, and `stat` is O(1).

**This job watches its own death.** A watcher nobody watches is the recursion the parent task
exists to break, so every pass rewrites `~/.claude/state/session-exit/keeper-heartbeat`, and
`exit-record-coverage.py` — which `fleet-board.py` already renders — reports it when it ages.
One existing reader, no second instrument. A long-lived process is only safe to run because
that heartbeat exists.

**The card is filed before the re-arm, and the re-arm runs only when the card exists.** The
other order hides the death: a re-armed pane reads `covered` again, so a card that failed to
reach the board would leave no trace that the writer ever died.

**Why this is not a step in `fleet-keeper.py`:** that script is pinned by `NoKillPathTest` to
hold no kill, resume, re-arm or spawn path (operator decision 2026-11-06, card `7653b7b6`).

Usage:
  exit-observer-keeper.py [--once] [--now EPOCH] [--dry-run]
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

PRODUCER_ID = "exit-observer-keeper"

POLL_SECONDS = 5        # between sweeps: one `stat` per watched pane, nothing else
REFRESH_SECONDS = 600   # how often the pane list is re-swept (one ~55 s sweep each)
OBSERVER_STALE = 45     # log silence that means the writer is gone (1.5 x its 30 s interval)
# ⚠️ Must stay BELOW `exit-record-coverage.py`'s KEEPER_STALE_SECONDS (300): a sweep measured
# ~55 s idle and >240 s under load, and if one blocks longer than that threshold the board
# reports the keeper itself as stale. 120 s is the honest ceiling.
SWEEP_TIMEOUT = 120


class Loop:
    """The watch, as a state machine one pass at a time — so a test can drive the clock.

    ⚠️ **The refresh is driven by elapsed time alone.** An earlier version also re-swept when
    the pane set came back empty, and an empty pane set is the HEALTHY fleet — the state this
    process spends nearly all its time in — so it swept continuously instead of every
    `REFRESH_SECONDS`. Nothing about the pane set's size may appear in the refresh decision.
    """

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.panes = {}
        self.unknown = 0
        self.refreshed = None  # None = never swept; any float is "swept at"
        self.rc = 0

    def due(self, now):
        """Is a sweep due? Time since the last sweep, and nothing else."""
        return self.refreshed is None or now - self.refreshed >= REFRESH_SECONDS

    def step(self, now):
        """One pass: beat, sweep if due, then `stat` each watched pane."""
        if not self.dry_run:
            beat(now)
        if self.due(now):
            result = sweep(now)
            if result is None:
                print("exit-observer-keeper: coverage sweep failed — cannot tell whether any "
                      "observer is dead; not reporting that as fine", file=sys.stderr)
                self.rc = 1
                # A failed sweep waits a FULL refresh interval. Retrying after 60 s re-ran a
                # ~55 s vault scan almost continuously, which kept the vault hot and every
                # sweep slow — the retry was the reason the failures persisted.
                self.refreshed = now
            else:
                self.panes, self.unknown = result
                self.refreshed = now
                if self.unknown:
                    print(f"exit-observer-keeper: the sweep could not resolve {self.unknown} "
                          f"manager pane(s) — they are NOT counted as healthy", file=sys.stderr)
                    self.rc = 1
                elif not self.panes:
                    print("exit-observer-keeper: every manager pane has a live writer")
        return check(now, self.panes, dry_run=self.dry_run) or self.rc


def beat(now):
    """Rewrite the liveness file the coverage sweep reads. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(f"{int(now)}\n")
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def sweep(now):
    """`({pane: {pid, session}}, unknown_count)` for manager panes, or `None` when it failed.

    ⚠️ `unknown_count` is carried out of here, never folded into "nothing to do": a sweep that
    cannot resolve a pane — under launchd with no `wezterm` on PATH it resolves none — returns
    an EMPTY `unrecorded` list, which is indistinguishable from a healthy fleet without it.
    """
    try:
        out = subprocess.run([sys.executable, COVERAGE, "--json"], capture_output=True,
                             text=True, timeout=SWEEP_TIMEOUT)
        doc = json.loads(out.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    panes = {}
    for row in list(doc.get("unrecorded", [])) + list(doc.get("unknown", [])):
        pane = row.get("pane")
        if not pane:
            continue
        ident = identity(pane)
        if ident:
            panes[str(pane)] = {"pid": ident[0], "session": ident[1],
                                "name": row.get("name", ""),
                                "session_id": row.get("session_id", "")}
    return panes, len(doc.get("unknown", []))


def identity(pane):
    """`(pid, session, last_beat_epoch)` from the pane's newest live spawn and its log mtime."""
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


def card(pane, name, session_id, beat_at):
    """File one `ack` card for this death. Returns the item id, or `None` when it did not land."""
    payload = (
        f"The exit observer for {name or '?'} (pane {pane}, session {str(session_id)[:8]}) has "
        f"died: the pane's last heartbeat is stale, so the session runs with no writer and its "
        f"next death would leave no record. The keeper re-arms it once this card exists."
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
    """Start a fresh observer for a live pane — the same call the spawn line makes."""
    try:
        out = subprocess.run([sys.executable, RECORD, "observe", "--pane", str(pane), "--pid", str(pid),
                              "--session", session], capture_output=True, timeout=SWEEP_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return out.returncode == 0


def check(now, panes, dry_run=False):
    """One `stat` pass over the watched panes. Returns the exit code for this pass."""
    rc = 0
    for pane, ident in panes.items():
        path = os.path.join(STATE_DIR, f"pane-{pane}.log")
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if age <= OBSERVER_STALE:
            continue
        if dry_run:
            print(f"exit-observer-keeper: would card and re-arm pane {pane} "
                  f"(pid {ident.get('pid')}, silent {int(age)} s)")
            continue
        item = card(pane, ident.get("name", ""), ident.get("session_id", ""),
                    int(os.stat(path).st_mtime))
        if not item:
            # Deliberately no re-arm: see the module docstring.
            print(f"exit-observer-keeper: pane {pane} card did not land — leaving it unrecorded "
                  f"rather than hiding the death", file=sys.stderr)
            rc = 1
            continue
        if not rearm(pane, ident.get("pid"), ident.get("session") or ""):
            print(f"exit-observer-keeper: pane {pane} carded ({item}) but the RE-ARM FAILED — it "
                  f"stays unrecorded", file=sys.stderr)
            rc = 1
            continue
        print(f"exit-observer-keeper: pane {pane} carded ({item}) and re-armed "
              f"(pid {ident.get('pid')})")
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="one sweep and one check pass, then exit")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen, act on nothing")
    args = ap.parse_args(argv)

    loop = Loop(dry_run=args.dry_run)
    while True:
        now = args.now if args.now is not None else time.time()
        rc = loop.step(now)
        if args.once:
            return rc
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
