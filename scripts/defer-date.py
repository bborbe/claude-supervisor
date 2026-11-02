#!/usr/bin/env python3
"""The shared `defer_date` reader — one parse for every script that asks "is it due yet?".

`defer_date` is defer-UNTIL: a task carrying one surfaces at that instant, not before.
Seven readers used to slice the first ten characters and compare dates, so a task
deferred to `2026-03-19T02:00:00+01:00` surfaced at midnight, and an offset could shift
the day. They now load this module by path (the filename carries a hyphen, scripts are
flat and not a package) and compare aware datetimes.

Accepted shapes:

  * `2026-11-02` (optionally quoted)          -> start of that day, LOCAL time
  * `2026-11-02T00:00:00Z` (vault-cli's RFC3339 rewrite of an unquoted date)
                                              -> start of that day, LOCAL time, so a
                                                 day-granular defer behaves exactly as a
                                                 date-only one
  * `2026-11-02T11:00:00+01:00` / `...Z`      -> that instant, offset kept
  * `2026-11-02T11:00:00` (no offset)         -> that wall-clock time, LOCAL
  * empty / None                              -> no defer

⚠️ **An unparseable value is `invalid`, never "available now".** The caller is told the
reason and decides; it must not read garbage as due. Five readers withhold the row;
`manager-quiet.py` is the one deliberate exception and fails toward ARMED instead — a
typo must not stand a live manager down — and says why on stderr.

⚠️ **Stricter than the `[:10]` slice it replaces.** Values the old slice silently
accepted — `2026-11-02 # note`, `2026-11-02 00:00:00 +01:00`, `2026-11-2` — are now
`invalid` and withheld by the fail-closed readers, so a hand-edited value of that shape
needs correcting in the vault rather than being half-read.
"""

import datetime as _dt
import re

_DATE_ONLY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MIDNIGHT_UTC = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ]00:00(?::00(?:\.0+)?)?Z$")


def _local_midnight(y, m, d):
    return _dt.datetime(y, m, d).astimezone()


def _clean(value):
    if value is None:
        return ""
    return str(value).strip().strip("'\"").strip()


def parse_defer_date(value):
    """`(aware datetime, None)`, `(None, None)` for no defer, or `(None, reason)`."""
    text = _clean(value)
    if not text:
        return None, None
    for pattern in (_DATE_ONLY, _MIDNIGHT_UTC):
        m = pattern.match(text)
        if m:
            try:
                return _local_midnight(*map(int, m.groups())), None
            except ValueError as exc:
                return None, f"defer_date {text!r} is not a valid date: {exc}"
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = _dt.datetime.fromisoformat(iso)
    except ValueError:
        return None, f"defer_date {text!r} is not an ISO date or datetime"
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive -> local wall-clock time
    return parsed, None


def as_now(now=None):
    """Normalise a caller's `now` to an aware datetime.

    None -> the current local time; a `date` or a date-only string -> start of that local
    day (the old `--today` semantics); a datetime string or object -> that instant (naive
    is local). Raises ValueError on an unparseable string.
    """
    if now is None:
        return _dt.datetime.now().astimezone()
    if isinstance(now, _dt.datetime):
        return now if now.tzinfo else now.astimezone()
    if isinstance(now, _dt.date):
        return _local_midnight(now.year, now.month, now.day)
    if isinstance(now, (tuple, list)) and len(now) >= 3:
        return _local_midnight(int(now[0]), int(now[1]), int(now[2]))
    parsed, reason = parse_defer_date(now)
    if parsed is None:
        raise ValueError(reason or f"empty now: {now!r}")
    return parsed


def defer_state(value, now=None):
    """`(state, detail)` — state is `deferred` | `due` | `none` | `invalid`.

    `deferred` means the parsed instant is strictly after `now`; `detail` is the parsed
    datetime for `deferred`/`due`, the reason for `invalid`, None for `none`.
    """
    parsed, reason = parse_defer_date(value)
    if reason:
        return "invalid", reason
    if parsed is None:
        return "none", None
    return ("deferred" if parsed > as_now(now) else "due"), parsed


def display(value):
    """What a reader is shown: `YYYY-MM-DD` for a day-granular defer, else ISO 8601."""
    parsed, _ = parse_defer_date(value)
    if parsed is None:
        return ""
    text = _clean(value)
    if _DATE_ONLY.match(text) or _MIDNIGHT_UTC.match(text):
        return parsed.date().isoformat()
    return parsed.isoformat()
