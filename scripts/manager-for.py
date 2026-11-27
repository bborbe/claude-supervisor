#!/usr/bin/env python3
"""Which live manager owns a goal — the CLI the approval gate calls.

One rule, not a second matcher: this delegates to `fleet-board.py:manager_for_goal`,
which reads `build_grouping` + `manager_subject`. The approval gate and the
board's `manager:` column therefore cannot disagree.

Liveness is `session-liveness.py --list --json`, the one instrument. ⚠️ The
registry alone is not enough: `fleet-board.registry_records()` returns every
entry, including a crashed session's lingering record, and a dead manager must
never satisfy an approval. The Fleet Manager is deliberately NOT a fallback —
it is live for almost every goal, so accepting it would make the gate pass
everywhere; the answer must be the goal's own manager or its declaring topic's.

Exit codes are a contract:
  0  the query ran; stdout carries the manager (or `null` / `no manager`)
  2  the fleet could not be read, or the vault path is not a directory — the
     answer is UNKNOWN and a caller must never read it as "no manager"

`task_titles` is passed empty on purpose: manager detection comes from
`manager_subject` (registry name → goal/topic page), never from a task title, so
the expensive all-vault task walk buys this query nothing.

Usage:
    manager-for.py --goal "<goal title>" --vault <vault path> [--json]
    manager-for.py --goal ... --vault ... --registry-dir <dir>   # tests
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_LIVENESS = os.path.join(HERE, "session-liveness.py")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_registry(registry_dir=None):
    """`{session id: {status, name, cwd}}` for the live sessions, or `None`.

    `None` is the two-state answer: the liveness read failed and the caller must
    refuse, never read it as "no manager". A partial list is not produced here —
    `session-liveness.py` owns that decision.
    """
    cmd = [sys.executable, SESSION_LIVENESS, "--list", "--json"]
    if registry_dir:
        cmd += ["--dir", registry_dir]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        records = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(records, list):
        return None
    return {
        r["sessionId"]: {
            "status": r.get("state") or r.get("status") or "",
            "name": r.get("name") or "",
            "cwd": r.get("cwd") or "",
        }
        for r in records
        if r.get("sessionId") and r.get("alive", True)
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal", required=True, help="goal title, without the wikilink brackets")
    ap.add_argument("--vault", required=True, help="absolute vault path to scope the index to")
    ap.add_argument("--json", action="store_true", help="emit one JSON object")
    ap.add_argument("--registry-dir", default=None, help="override the session registry path")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.vault):
        print("manager-for: vault not found: %s" % a.vault, file=sys.stderr)
        return 2

    registry = _live_registry(a.registry_dir)
    if registry is None:
        print("manager-for: live session read failed", file=sys.stderr)
        return 2

    fb = _load("fleet_board", "fleet-board.py")
    index = fb.vault_index([a.vault])
    name, source = fb.manager_for_goal(
        a.goal,
        index,
        registry,
        fb.colour_census(),
        fb.loop_slugs(),
        {},
    )
    if a.json:
        print(json.dumps({"goal": a.goal, "manager": name, "source": source}))
    else:
        print(name if name else "no manager")
    return 0


if __name__ == "__main__":
    sys.exit(main())
