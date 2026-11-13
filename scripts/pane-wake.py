#!/usr/bin/env python3
"""Wake an idle TAB session by typing into its pane — guarded, and dry-run by default.

`/supervisor:fleet-triage` needs to reach a live tab session that is idle at the prompt. A
cross-session `SendMessage` does not: it sits in the inbox until the session's next turn,
which an idle tab session never takes (measured 2026-11-08: 16 of 16 no-ops). And
`send_agent_message` reaches only roster tab workers — 4 of 39 live panes on the same day,
because a tab opened by `/supervisor:open` writes no ledger record. This script reaches
every tab session the way the reload lever does (`plugin-version-census.py` `reload_pane`):
session → pid → tty → pane, then a guarded keystroke.

**Dry-run is the default.** Without `--send` it prints, per session, the pid→tty→pane join,
the guard verdict and the exact text it would type — and types nothing.

Per session, in order (the first refusal wins, and every refusal is reported):

  skip:self              the session running this script, or its own pane
  skip:fleet-manager     the session registered as `Fleet Manager`
  skip:pusher            another session registered as `Fleet Pusher…` (idle between rounds by design)
  skip:not-live          the session is not in `session-liveness.py --list` as alive
  skip:no-pane           no pane joins to its pid (headless, or the join failed)
  skip:open-card         an open attention card names it — it is waiting on the operator
  <guard verdict>        any `skip:*` / `error:*` from `pane-wake-guard.py`, including
                         `skip:sleeping` for a subject `manager-liveness.py --state`
                         reports date-gated asleep

With `--send`, a session whose guard verdict is `wake` / `wake:suggestion`:

  delivered              the composer cleared AND the pane then showed a spinner
  unverified             typed, but no positive sign it acted after three bare Enters
  failed:<why>           activate / re-read / send-text failed, or the guard flipped on re-read

`unverified` is never success. Exit status: 0 when every session ended `dry-run`,
`delivered` or a `skip:*`; 1 when any ended `unverified` or `failed:*`; 2 on a read failure
that prevented classification.

Usage:
    pane-wake.py --session <id> [--session <id> ...]                  # dry-run
    pane-wake.py --session <id> --send --message "<text>"             # types
"""
import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ATTENTION_DIR = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/attention")
# ⚠️ `SUPERVISOR_WORKER_MANAGER_DIR` FIRST — it is the repo's established override for this
# directory (`scripts/fleet-register.py:33`, `scripts/answer-path-check.py:50`), and a reader
# that honours a different name than the writer is how a relocated tree reads as "no
# registration". `WORKER_MANAGER_STATE_DIR` stays as the fixture knob the tests set.
WORKER_MANAGER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)
FLEET_MANAGER_NAME = "Fleet Manager"
PUSHER_NAME = "Fleet Pusher"
DEFAULT_MESSAGE = (
    "Fleet wake: you are idle at the prompt with open work. Take the next unchecked subtask "
    "on your anchored task. Nothing was authorised on your behalf and this is not a green "
    "light past any gate."
)
ENTER_ATTEMPTS = 3
SETTLE_SECONDS = 1.5


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _load("plugin_version_census", "plugin-version-census.py")
guard = _load("pane_wake_guard", "pane-wake-guard.py")


def read_pane(pane_id):
    """Escape-laden visible screen, or None on a failed read."""
    try:
        res = subprocess.run(["wezterm", "cli", "get-text", "--escapes", "--pane-id", str(pane_id)],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def _poll(item_id):
    """`attention-ask.py poll` → its first line, or None when it could not run."""
    try:
        res = subprocess.run([sys.executable, os.path.join(HERE, "attention-ask.py"), "poll", item_id],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (res.stdout or "").strip().splitlines()
    return out[0] if res.returncode == 0 and out else None


def open_card(session_id, attention_dir=ATTENTION_DIR, poll=_poll):
    """The first OPEN gate naming this session, or None.

    ⚠️ **The `.open.json` suffix does not mean open.** Measured 2026-11-08: 279 such files,
    every one carrying `cleared_by` (`UserPromptSubmit` 187, `SessionEnd` 61, tool events
    the rest) — a glob-only test refused 35 of 61 live sessions as "carded". A file is a live
    gate only when `cleared_by` is absent, and then the store is the authority: its
    `item_id` is polled (`commands/fleet-triage.md` § INLINE, "read each matched file's
    `item_id` and poll that"). A poll that cannot run counts as open — the safe direction.
    """
    for path in sorted(glob.glob(os.path.join(attention_dir, f"{session_id}*.open.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            return os.path.basename(path)          # unreadable: never guess it is clear
        if rec.get("cleared_by"):
            continue
        item = rec.get("item_id")
        if not item:
            return os.path.basename(path)
        state = poll(item)
        if state is None or state.startswith("OPEN"):
            return item
    return None


def subject_of(session_id, state_dir=WORKER_MANAGER_DIR):
    """`(subject, branch)` from the session's worker-manager record, or None.

    The record is written when a worker is dispatched (`~/.claude/state/worker-manager/
    <session-id>.json`). A session without one resolves no subject — a fleet-manager tab
    opened by hand, or one dispatched before the record existed — and `None` here is an
    honest "unknown", never a claim that it is not asleep.
    """
    try:
        with open(os.path.join(state_dir, f"{session_id}.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    subject, branch = rec.get("subject"), rec.get("branch")
    if not subject or not branch:
        return None
    return subject, branch


def read_subject_state(session_id, *, subject_of=subject_of, run=subprocess.run):
    """The session's subject state, `guard.STATE_UNREADABLE`, or `None`.

    Three outcomes, and the difference between the last two is the whole point:

    - a state string — the read succeeded;
    - `guard.STATE_UNREADABLE` — a subject RESOLVED but the read failed (non-zero exit,
      unparsable line, timeout). The guard refuses the wake on this, because a missed
      wake re-arms a manager while a spurious one closes its pane for good;
    - `None` — NO subject resolved (no record, or a record without subject/branch). The
      screen verdict decides, exactly as before this parameter existed.

    ⚠️ **A failed read must never be representable as a state.** Never `""`, `"OK"`, or
    an empty dict: the guard would read a fabricated `OK` as "awake" and a fabricated
    `SLEEPING` would suppress every wake. And never collapse `STATE_UNREADABLE` into
    `None` — that is the fail-open that let three wakes through on 2026-11-13.
    """
    pair = subject_of(session_id)
    if pair is None:
        return None
    subject, branch = pair
    try:
        res = run([sys.executable, os.path.join(HERE, "manager-liveness.py"),
                   "--state", "--subject", subject, "--branch", branch],
                  capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return guard.STATE_UNREADABLE
    lines = (res.stdout or "").strip().splitlines()
    if res.returncode != 0 or not lines:
        return guard.STATE_UNREADABLE
    return lines[0].split("\t", 1)[0] or guard.STATE_UNREADABLE


def plan(session_ids, *, records, panes, pid_ttys, read, card, state,
         self_session=None, self_pane=None):
    """One row per requested session: where it joins, and what the guard says. Pure given
    its inputs — every read is passed in, so the target selection is testable without a
    fleet.

    `records`: the `session-liveness.py --list --json` rows. `panes`: `{pane_id: {"tty"}}`.
    `pid_ttys`: `{pid: tty}`. `read(pane_id)` → screen text. `card(session_id)` → card or None.
    `state(session_id)` → a state string; `guard.STATE_UNREADABLE` when a subject resolved
    but the read failed (refused); or None when NO subject resolved (screen decides).
    """
    by_sid = {r.get("sessionId"): r for r in records}
    by_tty = {}
    for pane_id, info in panes.items():
        tty = (info.get("tty") or "").replace("/dev/", "")
        if tty:
            by_tty.setdefault(tty, pane_id)

    rows = []
    for sid in session_ids:
        row = {"session_id": sid, "pid": None, "tty": None, "pane": None, "verdict": None}
        rec = by_sid.get(sid)
        if rec is not None:
            row["pid"] = rec.get("pid")
        if sid == self_session:
            row["verdict"] = "skip:self"
        elif rec is not None and (rec.get("name") or "").strip() == FLEET_MANAGER_NAME:
            row["verdict"] = "skip:fleet-manager"
        elif rec is not None and (rec.get("name") or "").strip().startswith(PUSHER_NAME):
            # Another pusher is idle between its own rounds by design; waking it starts a round
            # nobody scheduled, and two pushers waking each other is a loop.
            row["verdict"] = "skip:pusher"
        elif rec is None or not rec.get("alive"):
            row["verdict"] = "skip:not-live"
        else:
            row["tty"] = pid_ttys.get(rec.get("pid"))
            row["pane"] = by_tty.get(row["tty"]) if row["tty"] else None
            if row["pane"] is None:
                row["verdict"] = "skip:no-pane"
            elif self_pane is not None and str(row["pane"]) == str(self_pane):
                row["verdict"] = "skip:self"
            else:
                c = card(sid)
                if c:
                    row["verdict"] = "skip:open-card"
                    row["card"] = c
                else:
                    row["subject_state"] = state(sid)
                    row["verdict"] = guard.verdict(read(row["pane"]),
                                                   subject_state=row["subject_state"])
        rows.append(row)
    return rows


def _composer_clear(text):
    """True when the composer is empty or holds only a dim suggestion."""
    return census.pane_is_ready(text)


def send(pane_id, message, *, run=subprocess.run, read=read_pane, sleep=time.sleep,
         subject_state=None):
    """Type `message` into a pane the guard just cleared, then confirm POSITIVELY.

    `subject_state` is the plan's reading, reused rather than re-read: the guard's
    re-check exists to catch a MODAL that opened since the plan, and a second
    `manager-liveness.py` subprocess per wake would double the read for a value that
    cannot change between two keystrokes.

    The guard is re-run immediately before the keystrokes: the plan's read can be seconds
    old, and a modal that opened since is exactly what must not be typed into. Nothing is
    activated — `send-text --pane-id` reaches a background tab, and activating would take
    the operator's focus mid-keystroke (measured 2026-11-09). Text and Enter go separately,
    and a bare `\\r` is repeated up to three times — the trailing Enter is routinely
    swallowed (measured 2026-09-15, 2026-09-20).
    """
    before = read(pane_id)
    v = guard.verdict(before, subject_state=subject_state)
    if not v.startswith("wake"):
        return f"failed:guard-flipped {v}"

    try:
        typed = run(["wezterm", "cli", "send-text", "--pane-id", str(pane_id), "--no-paste", message],
                    capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed:send-text {exc}"
    if typed.returncode != 0:
        return f"failed:send-text exit {typed.returncode}"

    for _ in range(ENTER_ATTEMPTS):
        run(["wezterm", "cli", "send-text", "--pane-id", str(pane_id), "--no-paste", "\r"],
            capture_output=True, text=True, timeout=20)
        sleep(SETTLE_SECONDS)
        after = read(pane_id)
        if after is None:
            continue
        # Positive evidence only: the box is clear AND the session is visibly working.
        if guard.verdict(after) == "skip:spinner" and _composer_clear(after):
            return "delivered"
    return "unverified"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", action="append", required=True, help="target session id (repeatable)")
    ap.add_argument("--send", action="store_true", help="actually type; without it this is a dry run")
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    args = ap.parse_args(argv)

    records, err = census.live_sessions()
    if records is None:
        print(json.dumps({"error": f"liveness: {err}"}))
        return 2
    panes = census.wezterm_panes()
    if panes is None:
        print(json.dumps({"error": "wezterm cli list failed"}))
        return 2
    pids = [r.get("pid") for r in records if r.get("pid")]
    pid_ttys = census._pid_ttys(pids)
    if pid_ttys is None:
        print(json.dumps({"error": "ps failed"}))
        return 2

    rows = plan(args.session, records=records, panes=panes, pid_ttys=pid_ttys,
                read=read_pane, card=open_card, state=read_subject_state,
                self_session=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                self_pane=os.environ.get("WEZTERM_PANE"))

    rc = 0
    for row in rows:
        if row["verdict"] and row["verdict"].startswith("wake"):
            if args.send:
                row["outcome"] = send(row["pane"], args.message,
                                      subject_state=row.get("subject_state"))
                if row["outcome"] != "delivered":
                    rc = max(rc, 1)
            else:
                row["outcome"] = "dry-run"
                row["would_send"] = args.message
        else:
            row["outcome"] = row["verdict"]
            if row["verdict"] and row["verdict"].startswith("error:"):
                rc = max(rc, 2)
        print(json.dumps(row, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
