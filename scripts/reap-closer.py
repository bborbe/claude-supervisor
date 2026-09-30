#!/usr/bin/env python3
"""Has this session already run its close? The reap test's fourth read.

The drive leg's reap test (`agents/manager-drive.md` <process> step 1) decides that a task is
finished from three disk facts: `status: completed`, `phase: done`, zero open boxes. Those facts
say nothing about the SESSION, so a worker that has already run its close and is merely still
open reads exactly like one parked on the close gate. Measured 2026-09-27: one Manager Layer tick
reaped two such sessions. One had closed clean at 08:07:23Z, so its notice was stale; the other
still held `pick — 1. /vault-cli:session-close`, so its notice was correct.

This script reads the session's last closer: the last `👤 You:` line in its latest assistant
message carrying one, found in the transcript by session id.

  <session-id>   CLOSED (exit 0) / OPEN (exit 1) / UNREADABLE (exit 2)

Prints one line: `closer: <VERDICT> <timestamp> <line>`, or `closer: UNREADABLE (<why>)`.

⚠️ **Only CLOSED may withhold a reap.** OPEN and UNREADABLE both mean "reap as before". A read
that fails must never suppress a notice, which is why UNREADABLE is its own exit code rather than
a flavour of CLOSED.

⚠️ **Assistant messages only.** User turns carry injected command bodies (`/vault-cli:session-close`
prints its own closer templates), and a naive grep matches those.

⚠️ **Paths are deduped by realpath.** A symlinked project dir (a renamed vault) lists the same
transcript twice. Without the dedupe, every lookup is ambiguous, reads as UNREADABLE, and the fix
never fires. Found on the first live replay, 2026-09-30.

⚠️ **Not a repeat-count.** Nothing here counts notices. An open gate stays OPEN however often it
has been told.
"""

import glob
import json
import os
import re
import sys

CLOSED, OPEN, UNREADABLE = 0, 1, 2
WINDOW = 262144  # bytes read from the tail; a closed session is idle, so its closer is at the end
CLOSED_RE = re.compile(r"👤 You:\s*`?nothing — session closed")


def find_transcript(session_id, projects_dir):
    paths = {
        os.path.realpath(p)
        for p in glob.glob(os.path.join(projects_dir, "*", f"{session_id}.jsonl"))
    }
    return sorted(paths)


def last_closer(path):
    with open(path, "rb") as f:
        size = os.path.getsize(path)
        f.seek(max(0, size - WINDOW))
        tail = f.read().decode("utf-8", "replace")
    last = None
    for line in tail.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        content = d.get("message", {}).get("content", [])
        if isinstance(content, str):
            text = content
        else:
            text = "".join(
                x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text"
            )
        you = [ln.strip() for ln in text.splitlines() if "👤 You:" in ln]
        if you:
            last = (d.get("timestamp") or "?", you[-1])
    return last


def classify(session_id, projects_dir):
    """Return (exit_code, output_line)."""
    if not re.fullmatch(r"[0-9A-Za-z-]+", session_id or ""):
        return UNREADABLE, "closer: UNREADABLE (bad session id)"
    paths = find_transcript(session_id, projects_dir)
    if len(paths) != 1:
        return UNREADABLE, f"closer: UNREADABLE (transcripts found: {len(paths)})"
    try:
        last = last_closer(paths[0])
    except OSError as e:
        return UNREADABLE, f"closer: UNREADABLE ({e.__class__.__name__})"
    if last is None:
        return UNREADABLE, "closer: UNREADABLE (no closer in window)"
    ts, line = last
    if CLOSED_RE.search(line):
        return CLOSED, f"closer: CLOSED {ts} {line}"
    return OPEN, f"closer: OPEN {ts} {line}"


def main(argv):
    if len(argv) != 2:
        print("usage: reap-closer.py <session-id>", file=sys.stderr)
        return UNREADABLE
    projects_dir = os.environ.get("REAP_CLOSER_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")
    code, out = classify(argv[1], projects_dir)
    print(out)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
