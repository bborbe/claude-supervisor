#!/usr/bin/env python3
"""The stand-down marker's ORDERING rule — one implementation, two readers.

`/supervisor:stop` writes `stopped: true` plus `stop_date` onto a task page through
`scripts/task-standdown.py`. Two readers consult that marker: the open queue's
`is_stopped()` (`scripts/open-queue.py`) and the keepalive's resume decision
(`scripts/worker-keepalive.py`). Both tested the field for truthiness and nothing
else, so a row the operator re-flagged after stopping it stayed shut.

The rule is ORDERING, not presence: a row is stopped only when its `stop_date` is
strictly NEWER than the instant its flag was set. A re-flag is the operator's
newest word and reopens the row.

Measured 2026-11-24 on `Classify the Heat-Pump Penetration Seal`: flag cleared
17:10, stopped `2026-11-24T18:05:33+01:00`, re-flagged
`flag_set_at: 2026-11-24T19:36:58.408271+01:00`. A presence-only guard ignored
that last write.

⚠️ **The flag instant is the page's raw `flag_set_at`, never
`scripts/flag_provenance.py`'s `approval_instant()`.** That helper returns
`approved_at` first and answers "when was this approved", which is a different
instant from "when was this flagged".

⚠️ **Absent evidence never reopens a retired row.** `stopped: true` with no
`stop_date`, or with no parseable `flag_set_at`, keeps the presence-only skip: the
operator's retirement stands unless the page carries a comparison that overrides it.

⚠️ **Why a shared module rather than two identical literals.** The readers cannot
import each other — they are loaded by path, so `scripts/` is not on `sys.path`.
This module is loaded by path too (`importlib.util.spec_from_file_location`, the
same way `worker-keepalive.py` loads `topic-keepalive.py`), so the rule lives in
one file and the two readers cannot drift apart.
"""
from __future__ import annotations

from datetime import datetime

STOPPED_KEY = "stopped"
STOP_DATE_KEY = "stop_date"
FLAG_SET_AT_KEY = "flag_set_at"

#: The truthy set both readers have always used. Kept here so the marker's two
#: halves — presence and ordering — are decided in one place.
TRUTHY = {"true", "yes", "on"}


def parse_instant(value):
    """An aware `datetime` for a frontmatter timestamp, or None when unreadable.

    Accepts the shapes the vault writes: RFC3339 with an offset, a trailing `Z`, and
    a naive value (read as local time, the way the vault's own writers mean it). An
    empty or unparseable value is None — never "now", which would let a malformed
    page reopen itself.
    """
    text = (value or "").strip().strip("\"'")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def is_stopped(block, scalar) -> bool:
    """True when the stand-down marker still stands for this row.

    `scalar(block, key)` is the reader's own frontmatter accessor — `field` in
    `open-queue.py`, `topic-keepalive.scalar` in `worker-keepalive.py` — so this
    module never parses frontmatter itself and each reader keeps its own rules.

    True (skip the row) when `stopped` is truthy AND either timestamp is missing or
    unparseable, or the stop is strictly newer than the flag. A `stop_date` equal to
    the flag instant is not newer, so the tie opens the row.
    """
    if (scalar(block, STOPPED_KEY) or "").strip().lower() not in TRUTHY:
        return False
    stopped_at = parse_instant(scalar(block, STOP_DATE_KEY))
    if stopped_at is None:
        return True
    flagged_at = parse_instant(scalar(block, FLAG_SET_AT_KEY))
    if flagged_at is None:
        return True
    return stopped_at > flagged_at
