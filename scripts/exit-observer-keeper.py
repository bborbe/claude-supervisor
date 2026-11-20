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

**A routine death is recorded on disk, not carded.** The keeper appends one line to
`keeper-rearms.jsonl` before it re-arms, so a death that re-arms cleanly needs no operator
action and never reaches the board (operator decision 2026-11-19). It files an `ack` card only
when the record cannot be written or the re-arm fails — the card is the fallback trace, so a
death is never silent. This supersedes the earlier "card is filed before the re-arm" rule.

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

LEDGER = "keeper-rearms.jsonl"
# A re-arm counts as restored only when `beat_at` moves within this window. It equals
# `OBSERVER_STALE` on purpose: a re-armed observer writes its first heartbeat immediately, so a
# pane still stale a full stale-window after the re-arm was not restored.
VERIFY_SECONDS = OBSERVER_STALE

# Verification state for callers that carry none of their own (`check()` called directly).
_ARMED = {}


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
        # {(pane, beat_at): re-armed-at-epoch | "carded"} — the re-arm verification state
        # `check()` carries between passes. In-memory on purpose: the durable half is the
        # ledger, which dedups the record across a restart; this only stops a re-post.
        self.armed = {}

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
        return check(now, self.panes, dry_run=self.dry_run, armed=self.armed) or self.rc


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
    except subprocess.TimeoutExpired:
        print(f"exit-observer-keeper: sweep TIMEOUT after {SWEEP_TIMEOUT}s", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"exit-observer-keeper: sweep could not start: {exc}", file=sys.stderr)
        return None
    except ValueError as exc:
        # `text=True` decodes as it reads, and a non-UTF-8 byte raises UnicodeDecodeError —
        # itself a ValueError — from inside the call rather than from `json.loads`.
        print(f"exit-observer-keeper: sweep output could not be decoded: {exc}", file=sys.stderr)
        return None
    # ⚠️ The return code and stderr are logged, not discarded. Four separate theories about why
    # this sweep failed (blocking read, enumeration cost, host load, interpreter) were all
    # plausible and all wrong, because this path used to print one generic line and throw the
    # reason away. One line of real error text ends that guessing.
    if out.returncode != 0:
        print(f"exit-observer-keeper: sweep exited {out.returncode}: "
              f"{(out.stderr or '').strip()[:400]}", file=sys.stderr)
        return None
    try:
        doc = json.loads(out.stdout)
    except ValueError:
        print(f"exit-observer-keeper: sweep output is not JSON: {out.stdout[:200]!r}", file=sys.stderr)
        return None
    panes = {}
    # ⚠️ `recorded` is in this union on purpose. Building the watch set from `unrecorded` and
    # `unknown` alone watches only the panes ALREADY broken, so the first death of a healthy
    # writer is invisible until the next full sweep — up to `REFRESH_SECONDS`. Measured
    # 2026-11-18: the watch set was empty while eight manager panes carried live writers.
    for row in (list(doc.get("unrecorded", [])) + list(doc.get("unknown", []))
                + list(doc.get("recorded", []))):
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


def ledger_pairs():
    """Every `(pane, beat_at)` already in the re-arm ledger, read back from disk.

    ⚠️ This — not an in-memory set — is the dedup state, because the keeper is restarted by
    `launchctl kickstart` while a dead pane is still stale: memory-only dedup would re-record
    every such death once per restart. A torn or unreadable line is skipped rather than raised;
    one bad line must not blind the dedup for every other death.
    """
    pairs = set()
    try:
        with open(os.path.join(STATE_DIR, LEDGER), encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    pairs.add((str(row["pane"]), int(row["beat_at"])))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return pairs


def record(pane, session_id, beat_at):
    """Append one line to the re-arm ledger, at most once per `(pane, beat_at)`.

    The dedup key is `(pane, beat_at)` — the pane id and the mtime of its last heartbeat — and
    it is read back from the ledger, so one death is one line however many passes see it, and
    a keeper restart cannot re-record a death already on disk.

    This is the durable trace a routine death leaves instead of a board card. It is written
    BEFORE the re-arm: a re-armed pane reads `covered` again, so a record that landed after the
    re-arm would be the only sign the writer ever died — and losing it would lose the death.

    Returns True when the death is in the ledger — written now, or already there from an
    earlier pass — and False only when the line could not be written.
    """
    if (str(pane), int(beat_at)) in ledger_pairs():
        return True
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, LEDGER), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pane": str(pane), "session": session_id or "",
                                     "beat_at": int(beat_at)}) + "\n")
        return True
    except OSError:
        return False


def card(pane, name, session_id, beat_at):
    """File one `ack` card for this death. Returns the item id, or `None` when it did not land."""
    payload = (
        f"The exit observer for {name or '?'} (pane {pane}, session {str(session_id)[:8]}) has "
        f"died: the pane's last heartbeat is stale, so the session runs with no writer and its "
        f"next death would leave no record. The keeper's on-disk record or its re-arm failed, "
        f"so this card is the trace."
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


def check(now, panes, dry_run=False, armed=None):
    """One `stat` pass over the watched panes. Returns the exit code for this pass.

    **One death, one ledger line, one card.** A death is keyed `(pane, beat_at)` — the pane id
    and the mtime of its last heartbeat — and `record()` dedups on that key read back from the
    ledger, so a keeper restart cannot re-record a death it already wrote.

    **A re-arm is verified, never assumed.** `rearm()` returning rc 0 does not mean an observer
    is running: `session-exit-record.py observe` returns 0 when its daemonise fails, having
    started nothing. So the pass that sees a new death records it and re-arms, and a LATER pass
    cards it only if `beat_at` still has not moved `VERIFY_SECONDS` after that re-arm — a re-arm
    that returned rc 0 while the pane stayed stale is an un-re-armable death, the one case a
    ledger line alone would hide. The card is deduped by
    `exit-observer-silent-<pane>-<beat_at>`; `armed` stops this process re-posting it every
    pass, and a restart may re-post once (the store dedups by key).

    `armed` is `{(pane, beat_at): re-armed-at-epoch | "carded"}`, carried between passes. When
    omitted the module-level map is used, so a direct caller still behaves sanely.
    """
    rc = 0
    if armed is None:
        armed = _ARMED
    for pane, ident in panes.items():
        path = os.path.join(STATE_DIR, f"pane-{pane}.log")
        try:
            beat_at = int(os.stat(path).st_mtime)
        except OSError:
            continue
        key = (str(pane), beat_at)
        if now - beat_at <= OBSERVER_STALE:
            # A fresh log means the observer is writing again: every death of this pane is
            # over, including one recorded at an earlier beat_at.
            for stale in [k_ for k_ in armed if k_[0] == str(pane)]:
                armed.pop(stale, None)
            continue
        if dry_run:
            print(f"exit-observer-keeper: would record and re-arm pane {pane} "
                  f"(pid {ident.get('pid')}, silent {int(now - beat_at)} s)")
            continue
        state = armed.get(key)
        if state == "carded":
            continue
        if state is not None and now - state < VERIFY_SECONDS:
            continue  # re-armed moments ago; give the observer its window
        if state is not None:
            # Re-armed a full window ago and the log has not moved: the re-arm did not restore
            # the writer. Card it once and stop retrying this death.
            item = card(pane, ident.get("name", ""), ident.get("session_id", ""), beat_at)
            if item is not None:
                armed[key] = "carded"
                print(f"exit-observer-keeper: pane {pane} RE-ARM DID NOT RESTORE "
                      f"(beat_at {beat_at} unchanged {int(now - state)} s after re-arm, "
                      f"carded {item})", file=sys.stderr)
                rc = 1
            continue
        recorded = record(pane, ident.get("session_id", ""), beat_at)
        item = None
        if not recorded:
            # The ledger is the trace; if it cannot be written, the card is the fallback.
            item = card(pane, ident.get("name", ""), ident.get("session_id", ""), beat_at)
        rearm_ok = rearm(pane, ident.get("pid"), ident.get("session") or "")
        if not rearm_ok:
            if not item:
                item = card(pane, ident.get("name", ""), ident.get("session_id", ""), beat_at)
            if item is not None:
                armed[key] = "carded"
            print(f"exit-observer-keeper: pane {pane} RE-ARM FAILED "
                  f"({'carded ' + item if item else 'no card landed'})", file=sys.stderr)
            rc = 1
            continue
        if not recorded and item is None:
            print(f"exit-observer-keeper: pane {pane} re-armed with no record and no card",
                  file=sys.stderr)
            rc = 1
            continue
        armed[key] = now
        if item:
            print(f"exit-observer-keeper: pane {pane} carded ({item}) and re-armed "
                  f"(pid {ident.get('pid')})")
        else:
            print(f"exit-observer-keeper: pane {pane} recorded and re-armed "
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
