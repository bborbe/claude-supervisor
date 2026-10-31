#!/usr/bin/env python3
"""Has this session already run its close? The reap test's fourth read.

The drive leg's reap test (`agents/goal-drive.md` <process> step 1) decides that a task is
finished from three disk facts: `status: completed`, `phase: done`, zero open boxes. Those facts
say nothing about the SESSION, so a worker that has already run its close and is merely still
open reads exactly like one parked on the close gate. Measured 2026-09-27: one Manager Layer tick
reaped two such sessions. One had closed clean at 08:07:23Z, so its notice was stale; the other
still held `pick — 1. /vault-cli:session-close`, so its notice was correct.

This script reads the session's last closer: the last `👤 You:` line in its latest assistant
message carrying one, found in the transcript by session id.

  <session-id>   CLOSED (exit 0) / OPEN (exit 1) / UNREADABLE (exit 2) / PARKED (exit 3)

Prints one line: `closer: <VERDICT> <timestamp> <line>`, or `closer: UNREADABLE (<why>)`.

⚠️ **Only CLOSED and PARKED may withhold a reap.** OPEN and UNREADABLE both mean "reap as before".
A read that fails must never suppress a notice, which is why UNREADABLE is its own exit code rather
than a flavour of CLOSED.

⚠️ **PARKED is a standing `approve:`/`pick` gate — a session parked on the operator's own
decision.** Operator ruling **2026-10-20** (attention card `92f0e524bdd5c9c0b09c063b06363575`,
`ANSWERED: option: stop reaping`): the gate's own attention card already reaches the operator, so
a reap notice is redundant, the session can only refuse it, and the refusal trains sessions to
discount the reap channel. ⚠️ **PARKED is not CLOSED** — one is "the close has run", the other is
"the operator has not answered yet" — so the caller must render them differently even though both
withhold the reap. ⚠️ **It is not a repeat-count either:** the suppression keys on the closer's
*kind*, first time and every time.

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

CLOSED, OPEN, UNREADABLE, PARKED = 0, 1, 2, 3
WINDOW = 262144  # bytes per backward chunk, not the whole read — see `last_closer`
MAX_SCAN = 4194304  # hard bound on the backward scan, so a hook never reads a whole transcript
CLOSED_RE = re.compile(r"👤 You:\s*`?nothing — session closed")
# A standing operator gate: the closer's own line carries `pick` or `approve`. Matched
# after CLOSED, so a `nothing — session closed` line can never reach here.
PARKED_RE = re.compile(r"👤 You:\s*`?(pick|approve)\b")


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


_TYPE_TEXT = re.compile(r'\{\s*"type"\s*:\s*"text"')
_TEXT_KEY = re.compile(r'"text"\s*:\s*"')
_ASSISTANT = re.compile(r'"type"\s*:\s*"assistant"')


def _json_string_at(text, start):
    """`(body, terminated)` for a JSON string whose opening quote ends at `start`.

    Stops at the closing quote or at truncation, and **never** mistakes an escaped
    `\\"` for the terminator. Cutting at the first `"` instead — as this did — dropped
    everything after a quoted `--subject`, including a trailing `--admin`, so the
    admin-merge deny check never fired.

    `terminated` is load-bearing: an unterminated body means the harness cut the line,
    so its tail may be half a command.
    """
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return text[start:i], True
        i += 1
    return text[start:], False


def _text_values(text):
    """`(body, terminated)` for each `text` content block of a partial assistant line.

    Mirrors the parsed path, which joins only `type == "text"` blocks — a closer quoted
    in a `tool_use` input or a `thinking` block is not the operator's gate. The `{`
    anchor narrows the match to a block start; ⚠️ it does not eliminate the residual
    where a structured `tool_use` input is itself shaped `{"type": "text", "text": …}`.
    """
    values, pos = [], 0
    while True:
        kind = _TYPE_TEXT.search(text, pos)
        if not kind:
            return values
        # Bounded to this block: an unbounded search could pair a truncated block's
        # marker with the NEXT block's text key and salvage the wrong string.
        key = _TEXT_KEY.search(text, kind.end(), kind.end() + 200)
        if not key:
            return values
        values.append(_json_string_at(text, key.end()))
        pos = key.end()


def _partial_closer(text):
    """`(timestamp, line)` recovered from a partially-written assistant line, or `None`.

    The harness streams a message to disk, so a `Stop` hook can read a line whose JSON is
    not closed yet. `json.loads` rejects it, and the pre-fix scan then SKIPPED it and
    returned an EARLIER message's closer — measured 2026-10-31, session `95e9b174`: a real
    `gh pr merge` gate was recorded as `not-a-gate` 266 ms after the message was written.

    The text is already on disk, so recover it rather than fall back to a stale message.
    """
    if not _ASSISTANT.search(text):
        return None
    for body, terminated in reversed(_text_values(text)):
        idx = body.rfind("👤 You:")
        if idx < 0:
            continue
        tail = body[idx:]
        if "\\n" in tail:
            # The closer's own line ended before the cut, so it is complete whatever
            # follows it.
            line = tail.split("\\n", 1)[0].strip()
        elif terminated:
            # The string closed cleanly: the closer is the message's last line.
            line = tail.strip()
        else:
            # ⚠️ The closer runs into an UNTERMINATED tail, so the harness may have cut
            # it mid-command — `--admi` must never be read as a whole closer, or the
            # `--admin` deny misses and the lint releases a branch-protection bypass.
            return None
        if not line:
            return None
        ts = re.search(r'"timestamp":\s*"([^"]+)"', text)
        return (ts.group(1) if ts else "?"), line
    return None


def last_closer(path, max_bytes=MAX_SCAN):
    """`(timestamp, line)` for the transcript's last `👤 You:` line, or `None`.

    Scans backwards from EOF and returns at the first assistant message carrying a
    closer, so the common case (closer at the end) costs one `WINDOW` read.
    """
    for raw in tail_lines(path, max_bytes):
        try:
            d = json.loads(raw)
        except ValueError:
            # ⚠️ A partial final line is the turn's own message, not a line to skip past.
            # Falling back to an older message here is the stale-closer defect: the hook
            # then decides on a closer the operator never saw.
            text = raw.decode("utf-8", "replace")
            salvaged = _partial_closer(text)
            if salvaged is not None:
                return salvaged
            if _ASSISTANT.search(text):
                # An assistant message we cannot read is not a licence to use an older one.
                return None
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
    if PARKED_RE.search(line):
        return PARKED, f"closer: PARKED {ts} {line}"
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
