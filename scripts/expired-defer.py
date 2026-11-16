#!/usr/bin/env python3
"""`expired-defer.py` — count tasks left at `in_progress` with a lapsed `defer_date`.

A task reading `status: in_progress` with a `defer_date` earlier than today is a dead
park: the date has passed, nothing moves the task, and `close-worker.py` refuses to close
it (`task-not-terminal`). It reads as live work to every reader and is invisible to every
rotation. Measured 2026-11-08 across the three task roots: 139 such rows. The population
had drained to 1 by 2026-11-15, so this is a **recurrence guard**, not a backlog repair —
its job is to make a future one impossible to miss, which is why the count rides the
fleet round's digest rather than a separate report nobody opens.

⚠️ **This script is the single parse.** `vault-cli`'s `EXPIRED_DEFER_DATE` lint rule
(`pkg/ops/lint.go`) and this counter must agree, so both apply the same three rules:

  * `status` is exactly `in_progress`;
  * the first ten characters of `defer_date` parse as `YYYY-MM-DD`, and that day is
    **earlier than today** — date-only, local time, so a defer later TODAY is not
    expired and an RFC3339 instant does not expire mid-day;
  * the two exemptions `HOLD_WITH_DEFER_DATE` already carries: `created_by:
    recurring-task-creator` and a non-empty `recurring:` key. For those the `defer_date`
    is a *schedule* date, not a snooze, so a past one is not a lapsed park.

An unparseable `defer_date` is not counted: this script cannot vouch for it, and the
vault's `STATUS_DATE_MISMATCH` lint already owns the malformed-date class.

⚠️ **No byte cap on the frontmatter read.** The vault-side regression probe this replaces
read only the first 2000 bytes of each file; a `defer_date` past that offset was invisible
to it while visible to the lint, which is exactly the two-instruments-disagree defect this
count exists to remove. The frontmatter block is read to its closing `---` delimiter.

Verbs:

  (default)            print the one line the digest carries: `expired-defer=<n>`
  --json               print `{"count": n, "roots": [...], "rows": [{path, defer_date}]}`
  --check              same scan, but exit 10 when n > 0 (a gate); exit 0 when clean

Exit status: 0 on a successful scan (including a non-zero count — a digest producer that
exits non-zero reads as a failed round), 10 for `--check` with a non-zero count, 2 on a
usage error. A root that does not exist is skipped **and named on stderr**, never silently
dropped: an unreadable root is not an empty one.
"""

import argparse
import datetime
import json
import os
import re
import sys

#: The three task roots the vault's regression metric names. `private-agent` keeps its
#: tasks under `tasks/`; the two Obsidian vaults use the numbered `25 Tasks/`.
DEFAULT_ROOTS = [
    os.path.expanduser("~/Documents/Obsidian/private-personal/25 Tasks"),
    os.path.expanduser("~/Documents/Obsidian/private-agent/tasks"),
    os.path.expanduser("~/Documents/Obsidian/seibert-personal/25 Tasks"),
]

_STATUS = re.compile(r"^status:\s*['\"]?([A-Za-z_]+)", re.M)
_DEFER = re.compile(r"^defer_date:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.M)
_CREATED_BY = re.compile(r"^created_by:\s*(.+)$", re.M)
_RECURRING = re.compile(r"^recurring:\s*(.*)$", re.M)

#: The exemption set, kept identical to `deferDateExempt` in vault-cli's `pkg/ops/lint.go`.
RECURRING_CREATOR = "recurring-task-creator"


def frontmatter(text):
    """The frontmatter block, or None when the file does not open with `---`.

    Read to the closing delimiter rather than to a byte cap — see the module docstring.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return None


def expired(block, today):
    """`defer_date` when this frontmatter is an expired, non-exempt park, else None."""
    status = _STATUS.search(block)
    if not status or status.group(1) != "in_progress":
        return None
    defer = _DEFER.search(block)
    if not defer:
        return None
    created_by = _CREATED_BY.search(block)
    if created_by and created_by.group(1).strip().strip("'\"") == RECURRING_CREATOR:
        return None
    recurring = _RECURRING.search(block)
    if recurring and recurring.group(1).strip().strip("'\""):
        return None
    try:
        when = datetime.date.fromisoformat(defer.group(1))
    except ValueError:
        return None
    return defer.group(1) if when < today else None


def scan(roots, today):
    """`(rows, missing)` — rows are `{"path", "defer_date"}` for every expired park."""
    rows, missing = [], []
    for root in roots:
        if not os.path.isdir(root):
            missing.append(root)
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        text = handle.read()
                except OSError as exc:
                    print("expired-defer: cannot read %s: %s" % (path, exc), file=sys.stderr)
                    continue
                block = frontmatter(text)
                if block is None:
                    continue
                defer_date = expired(block, today)
                if defer_date is not None:
                    rows.append({"path": path, "defer_date": defer_date})
    return rows, missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="task root to scan (repeatable; default: the three vault task roots)",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="YYYY-MM-DD to treat as today (default: the local date) — the injection "
        "point that makes this deterministic under test",
    )
    parser.add_argument("--json", action="store_true", help="detail instead of the count line")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 10 when the count is non-zero, for use as a gate",
    )
    args = parser.parse_args(argv)

    try:
        today = (
            datetime.date.fromisoformat(args.today)
            if args.today
            else datetime.date.today()
        )
    except ValueError:
        print("expired-defer: --today is not a YYYY-MM-DD date: %r" % args.today, file=sys.stderr)
        return 2

    roots = args.root or DEFAULT_ROOTS
    rows, missing = scan(roots, today)
    for root in missing:
        print("expired-defer: root does not exist, skipped: %s" % root, file=sys.stderr)

    if args.json:
        print(json.dumps({"count": len(rows), "roots": roots, "rows": rows}, indent=2))
    else:
        print("expired-defer=%d" % len(rows))

    if args.check and rows:
        return 10
    return 0


if __name__ == "__main__":
    sys.exit(main())
