#!/usr/bin/env python3
"""The permission-channel reading, shared by the watcher and the round.

`manager-attention-watch.py` decides whether a session's permission channel is
aborting; `round-readings.py` needs the same reading for a different population —
every id a row ever ran under, rather than the one tracked session. Keeping two
copies would put the window in two places, and a drift would be SILENT: too short
a window reads as "no aborts" rather than as an error.

⚠️ The two callers apply `PERM_CHANNEL_STALE_S` differently ON PURPOSE — the round
applies it, the watcher does not. See that constant's own comment before changing
either. Sharing the reader does not mean sharing the policy.

Extracted from `manager-attention-watch.py`, which re-exports every name here
unchanged, so no caller had to move.
"""

import datetime
import json
import os
import re

# Three aborts inside this window is a channel that cannot be trusted to carry an
# approval. The anchor is the transcript's OWN last stamp, never wall clock, so a
# frozen transcript's window never moves.
PERM_CHANNEL_WINDOW_S = 15 * 60
PERM_CHANNEL_MIN = 3

# How old a transcript may be before a caller stops reading it for the channel
# cause. ⚠️ **This guards the ROUND's carrier, not the watcher's.**
# `round-readings.py` walks every id a row ever ran under — `claude_session_id`
# plus each `metrics_sessions` entry — and an id from a session that died months
# ago keeps a transcript whose last 15 minutes of life ended in aborts. Measured
# on the first review of this change: without this bound such an id pins a healthy
# LIVE row to `permission-channel` forever, because the window is anchored on the
# transcript's own last stamp and a frozen transcript's window never moves.
#
# ⚠️ The watcher deliberately does NOT apply it. `probe()` reads the tracked task's
# own `claude_session_id`, and a session that is genuinely gone is settled by
# `is_gated`'s `unregistered` branch BEFORE the stuck limb — so there is no stale
# id for this bound to filter there. Applying it would instead create a blind spot
# on the one shape this cause exists for: a session whose channel is dead cannot
# act, so its transcript goes quiet, and a staleness bound would un-mark it exactly
# while it is failing.
PERM_CHANNEL_STALE_S = 2 * PERM_CHANNEL_WINDOW_S

# The abort text a failed permission request leaves in a `tool_result` block.
PERM_FAILURE_MARK = "permission request failed"

_TS_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def iso_epoch_any(value):
    """An ISO-8601 timestamp as epoch seconds, or None."""
    try:
        return datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def read_recent_tail(path, window_s, chunk=400_000, max_bytes=8_000_000):
    """The transcript tail spanning at least `window_s` of its OWN record time.

    ⚠️ **A fixed byte window is the wrong unit for the channel read, and the loss
    is measured.** On session `b6e447e4` (11.5 MB, measured 2026-11-09) the
    permission-channel aborts sit at lines 2862-3287 of 6324 — roughly the middle
    of the file — while a 400 KB tail reaches only the last 28 tool results. Every
    abort was outside it, so the count read ZERO and the session read
    `stuck_reason=None` while its writes were aborting. A byte window is a proxy
    for recency that fails in exactly the sessions worth watching: the long, busy
    ones.

    So this reader walks BACKWARD in chunks and stops once the collected text spans
    `window_s` of transcript time, or the file start, or `max_bytes`.

    Returns the decoded text, or None when it cannot be read. A transcript with no
    parseable timestamp yields whatever one chunk held — the caller's count is then
    simply not fired, which is the fail-safe direction.

    ⚠️ **Each chunk boundary splits one record in half**, because the join inserts a
    newline where the file had none. Both halves fail to parse and are skipped, so
    the cost is one unreadable record per chunk rather than a miscounted one.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    chunks = []
    end = size
    try:
        with open(path, "rb") as fh:
            while end > 0 and (size - end) < max_bytes:
                start = max(0, end - chunk)
                fh.seek(start)
                chunks.append(fh.read(end - start))
                end = start
                text = b"\n".join(reversed(chunks)).decode("utf-8", "replace")
                stamps = [iso_epoch_any(m) for m in _TS_RE.findall(text)]
                stamps = [s for s in stamps if s is not None]
                if stamps and max(stamps) - min(stamps) >= window_s:
                    break
    except OSError:
        return None
    return b"\n".join(reversed(chunks)).decode("utf-8", "replace")


def channel_aborts(tail, window=PERM_CHANNEL_WINDOW_S):
    """How many `Stream closed` aborts fall inside the tail's last `window` seconds.

    A WINDOWED reading, and the whole point is that it does NOT reset on a success.
    Clearing is by AGEING OUT, never by a success: three aborts inside 15 minutes is
    a channel that cannot be trusted to carry an approval, and a fourth tool that
    happened to run proves only that one tool ran.

    Returns 0 when the tail carries no parseable timestamp: an undated tail cannot
    establish recency, and firing on one would mark every transcript whose clock
    field this reader failed to parse.

    ⚠️ **The anchor is the tail's LAST record, never its last abort.** Anchoring on
    the aborts themselves would make the window self-referential: three aborts an
    hour ago would always sit inside "the last 15 minutes of abort time" and the
    cause would never clear.
    """
    if not tail:
        return 0
    aborts, stamps = [], []
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = iso_epoch_any(rec.get("timestamp") or "")
        if ts is None:
            continue
        stamps.append(ts)
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(blk, dict)
            and blk.get("type") == "tool_result"
            and blk.get("is_error")
            and PERM_FAILURE_MARK in str(blk.get("content") or "")
            and "Stream closed" in str(blk.get("content") or "")
            for blk in content
        ):
            aborts.append(ts)
    if not stamps:
        return 0
    anchor = max(stamps)
    return sum(1 for ts in aborts if ts >= anchor - window)
