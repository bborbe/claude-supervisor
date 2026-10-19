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
WINDOW = 262144  # bytes per backward chunk, not the whole read — see `last_closer`
MAX_SCAN = 4194304  # hard bound on the backward scan, so a hook never reads a whole transcript
CLOSED_RE = re.compile(r"👤 You:\s*`?nothing — session closed")


def find_transcript(session_id, projects_dir):
    paths = {
        os.path.realpath(p)
        for p in glob.glob(os.path.join(projects_dir, "*", f"{session_id}.jsonl"))
    }
    return sorted(paths)


def tail_lines(path, max_bytes=MAX_SCAN):
    """Yield the transcript's lines newest-first, scanning backwards in `WINDOW` chunks.

    ⚠️ **A fixed tail window is not enough, and the failure is silent.** Measured
    2026-10-19: a `Stop` hook called `last_closer` on a 15 MB session transcript 44 ms
    after the closer was written, and it returned `None` — the harness writes large
    `api-request-blob` entries after the assistant message, so the closer sat ~758 KB
    from EOF and a 256 KB read never reached it. The same defect reproduces with one
    400 KB entry after the closer. The scan is bounded (`max_bytes`) so a pathological
    transcript cannot turn a hook into a full-file read.
    """
    size = os.path.getsize(path)
    end = size
    carry = b""
    with open(path, "rb") as f:
        while end > 0 and size - end < max_bytes:
            # ⚠️ The FIRST chunk is clamped to the remaining budget: reading a full
            # `WINDOW` regardless would make `max_bytes` a no-op on the first iteration.
            chunk = min(WINDOW, max_bytes - (size - end))
            start = max(0, end - chunk)
            f.seek(start)
            data = f.read(end - start) + carry
            lines = data.split(b"\n")
            if start > 0:
                # The first line is the tail of one whose head is in the next chunk.
                carry = lines[0]
                lines = lines[1:]
            else:
                carry = b""
            for raw in reversed(lines):
                if raw.strip():
                    yield raw
            end = start


def last_closer(path, max_bytes=MAX_SCAN):
    """`(timestamp, line)` for the transcript's last `👤 You:` line, or `None`.

    Scans backwards from EOF and returns at the first assistant message carrying a
    closer, so the common case (closer at the end) costs one `WINDOW` read.
    """
    for raw in tail_lines(path, max_bytes):
        try:
            d = json.loads(raw)
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
            return d.get("timestamp") or "?", you[-1]
    return None


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
