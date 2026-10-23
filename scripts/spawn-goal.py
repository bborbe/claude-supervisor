#!/usr/bin/env python3
"""Arm a `/goal` condition in a freshly spawned pane, and fail closed.

The spawn path needs three things after it creates the pane and before it sends the work
command: emit the condition, submit it, and confirm the pane actually armed the goal. Prose
cannot enforce the third — a call site that sleeps and hopes leaves the row idling under an
armed goal, which is the failure the whole `goal_loop` feature exists to remove. So the
sequence lives here, where it can be checked, and the call site is one line that exits
non-zero.

Two refusals, both loud and both before anything is sent:
  * the emitter itself refuses (no Success Criteria, headless, a cluster target) — exit 1
  * the marker never appears within the timeout — exit 2, and the work command is not sent

The marker is the pane text `Goal set:` that Claude Code prints under the submitted
`/goal` line; it was measured on a live worker 2026-10-22 alongside `◎ /goal active`.

Usage:
  spawn-goal.py --pane <id> --task "<name>" --vault <v> [--timeout 60]

Exit codes: 0 armed · 1 the emitter refused · 2 the marker never appeared.
"""
import argparse
import pathlib
import subprocess
import sys
import time

EMITTER = pathlib.Path(__file__).resolve().parent / "goal-condition.py"
MARKER = "Goal set:"


def pane_text(pane):
    """The pane's visible text, or None when wezterm cannot read it.

    `None` and `""` are different states and must stay different: a broken read is not
    evidence of anything, while a reachable pane rendering nothing is. Both leave the
    marker absent, so the wait continues either way — but collapsing them would mean a
    dead mux socket reads exactly like a pane that has not printed yet, which is the
    three-state collapse `scripts/transport-read-check.py` exists to catch.
    """
    try:
        return subprocess.run(
            ["wezterm", "cli", "get-text", "--pane-id", str(pane)],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def send(pane, text):
    """Type text into the pane, then submit it as a separate step.

    The split is not decoration: a trailing `\\r` on the text does not submit reliably.
    """
    for payload in (text, "\r"):
        subprocess.run(
            ["wezterm", "cli", "send-text", "--pane-id", str(pane), "--no-paste", payload],
            check=True, timeout=10,
        )
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Arm a /goal in a spawned pane.")
    parser.add_argument("--pane", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    emitted = subprocess.run(
        [sys.executable, str(EMITTER), "--task", args.task, "--vault", args.vault],
        capture_output=True, text=True,
    )
    if emitted.returncode != 0:
        print(emitted.stderr.rstrip() or f"goal-condition exited {emitted.returncode}",
              file=sys.stderr)
        print("⛔ goal not set — work command not sent", file=sys.stderr)
        return 1

    try:
        send(args.pane, emitted.stdout)
    except (OSError, subprocess.SubprocessError) as err:
        print(f"spawn-goal: cannot type into pane {args.pane}: {err}", file=sys.stderr)
        print("⛔ goal not set — work command not sent", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if MARKER in (pane_text(args.pane) or ""):
            return 0
        time.sleep(1)

    print(f"spawn-goal: '{MARKER}' did not appear in pane {args.pane} within {args.timeout}s",
          file=sys.stderr)
    print("⛔ goal not set — work command not sent", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
