#!/usr/bin/env python3
"""Render the fleet digest's `unclaimable:` section.

A task whose `target_vault` is empty or absent is claimed by no controller: the
non-owning controller drops it as cross-vault traffic and the legacy-default one
cannot recreate a file already at the title path. Since agent-task-controller
v0.26.12 the create path refuses such a command outright, but a file written
before that guard — or by a producer that predates it — is still unclaimable,
and nothing reports it. This script is that report.

The digest's existing rows are session-keyed, and the defect case has no pod and
no session to key on, which is why this section is deliberately independent of
the roster: it reads the vault, not the fleet.

⚠️ The window is `created_date`, else `job_started_at` — NEVER file mtime.
Measured 2026-11-11: mtime is the clone date on most of these files, so an mtime
window reported 9,624 "recent" emissions when the true number was zero. A file
with neither field is UNDATED and counted separately; it cannot be placed in the
window at all.

⚠️ Only agent-minted records are in scope — a file carrying `task_type`. A
planning vault's hand-written rows legitimately have no `target_vault`, and
counting them added 368 false "recent" hits in the same measurement.

Usage:
    unclaimable-tasks.py                  # digest section, default window
    unclaimable-tasks.py --since 2026-11-01 --limit 20
    unclaimable-tasks.py --json
"""

import argparse
import glob
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SINCE = "2026-11-01"
RUNTIME_VAULTS = {"private-agent", "seibert-agent"}
FIELD = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UNREADABLE = []


def frontmatter(path):
    fields = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().split("\n")
    except OSError:
        # Recorded, not swallowed: an unreadable task file is skipped by every
        # sweep, so a silent skip here reads exactly like a clean scan.
        UNREADABLE.append(path)
        return fields
    if not lines or lines[0].strip() != "---":
        return fields
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = FIELD.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return fields


def window_date(fields):
    for key in ("created_date", "job_started_at"):
        value = fields.get(key, "")
        if value:
            return value[:10]
    return ""


def rows(since, roots=None):
    if roots:
        pass
    else:
        fleet = _load("fleet_sessions", "fleet-sessions.py")
        roots = []
        for path, subs in fleet.vault_dirs_from_cli().items():
            # Only the AGENT RUNTIME vaults. A planning vault's `25 Tasks/` holds
            # hand-written rows that legitimately carry no `target_vault`; some of
            # them set `task_type` too, and measured 2026-11-11 all 16 "recent"
            # hits came from there. The defect only exists for records an agent
            # minted, and those land in a runtime vault.
            if os.path.basename(path) not in RUNTIME_VAULTS:
                continue
            for sub in subs:
                candidate = os.path.join(path, sub)
                if os.path.isdir(candidate):
                    roots.append(candidate)
        # No fallback path list. A hardcoded home-directory guess is wrong on any
        # other machine and, worse, silently scans a directory that is not the
        # fleet's — so an empty resolution is a failed read, never an empty result.
        if not roots:
            raise SystemExit(
                "unclaimable-tasks: no agent runtime vault resolved from vault-cli config; "
                "refusing to report 'none' for a pass that scanned nothing"
            )

    found = []
    for root in roots:
        for path in glob.glob(os.path.join(root, "*.md")):
            fields = frontmatter(path)
            if not fields.get("task_type"):
                continue
            if fields.get("target_vault", "") != "":
                continue
            found.append(
                {
                    "file": os.path.basename(path),
                    "path": path,
                    "task_identifier": fields.get("task_identifier", ""),
                    "task_type": fields.get("task_type", ""),
                    "status": fields.get("status", ""),
                    "touched": window_date(fields),
                }
            )
    found.sort(key=lambda r: (r["touched"], r["file"]), reverse=True)
    recent = [r for r in found if r["touched"] and r["touched"] >= since]
    undated = [r for r in found if not r["touched"]]
    return found, recent, undated


def main():
    parser = argparse.ArgumentParser(description="Render the digest's unclaimable: section")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--limit", type=int, default=10, help="max recent rows to print")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", action="append", default=None,
                        help="directory to scan instead of the configured vaults (fixtures)")
    args = parser.parse_args()

    found, recent, undated = rows(args.since, args.root)

    if args.json:
        json.dump(
            {"since": args.since, "all_time": len(found), "recent": len(recent),
             "undated": len(undated), "rows": recent},
            sys.stdout, indent=2,
        )
        print()
        return 0

    print("unclaimable:")
    if not found:
        print("  none — no agent-minted task carries an empty target_vault")
        return 0
    for row in recent[: args.limit]:
        blocked = row["file"] if row["task_type"] == "pr-review" else "—"
        print(
            f"  {row['task_identifier'] or '—':<40} {row['task_type']:<18} "
            f"{row['status'] or '—':<12} {row['file']}  (blocks: {blocked})"
        )
    if len(recent) > args.limit:
        print(f"  … and {len(recent) - args.limit} more")
    # Undated rows are subtracted from `historical`, not counted in both. They are
    # a row set with no window date, so leaving them inside `historical` made the
    # three numbers exceed the total.
    counts = (
        f"  recent: {len(recent)} (since {args.since})"
        f" · historical: {len(found) - len(recent) - len(undated)}"
        f" · undated: {len(undated)}"
    )
    if UNREADABLE:
        counts += f" · unreadable: {len(UNREADABLE)}"
    print(counts)
    if recent:
        print("  ⚠️ recently minted — the mint is still producing the empty field")
    else:
        print("  no recent emissions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
