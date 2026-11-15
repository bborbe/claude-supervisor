#!/usr/bin/env python3
"""A topic or goal manager must never be running headless.

**Rule source: `docs/fleet-surface.md` § Spawn a worker item 6 — "A manager row never resumes
headless, and `role:` is read before `mode:`".** This script is the *detector* for that rule, not
a second statement of it; it never decides what should happen, it only names what is true now.

Why a detector at all: a headless manager has no tab, no pane and therefore no jump link. The
Vault UI still renders its card as **Live**, so the board says it is running and offers nothing
to click, and the operator has no way to see, watch or intervene. Measured 2026-11-14 on
`23 Topics/Development Pipeline Throughput.md`: the manager (session `6f14c290`) had been a tab
for four spawns, came back headless, and the operator asked why the topic was not open — it
**was** open and working, with nothing on screen to find.

  exit 0   clean — no live `sdk-cli` session owns a topic or goal page
  exit 1   at least one DEFECT line printed
  exit 2   the registry, the liveness probe or the vault could not be read — a refusal,
           never a clean census

⚠️ **`entrypoint` is not on the liveness record.** `session-liveness.py --list --json` returns
`sessionId`, `name`, `cwd`, `state` and friends — no `entrypoint` — so the join needs the
identity registry as well. Reading it from the liveness record would silently match nothing and
report a clean fleet, which is the failure shape this script exists to remove.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: The entrypoint a tab carries. A manager on this one is visible and reachable.
INTERACTIVE = "cli"

#: The entrypoint a headless session carries. A manager on this one is invisible.
HEADLESS = "sdk-cli"

#: The page kinds a manager owns. A task page's session is a worker, and a worker is
#: headless by design (`docs/role-mode-matrix.md` Rule 1) — never a defect here.
MANAGER_KINDS = ("topic", "goal")

PROBE_TIMEOUT = 30


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: `scripts/keeper-census.py` — the page scan and the liveness read, reused rather than copied.
#: A second reader of either would be a second answer to the same question.
CENSUS = _load("keeper_census", "keeper-census.py")


def default_registry():
    return os.environ.get("SUPERVISOR_SESSIONS_DIR") or os.path.expanduser("~/.claude/sessions")


def entrypoints(directory):
    """`{session_id: entrypoint}` from the identity registry, or `None` when unreadable.

    `None` is a refusal: an unreadable registry must never render as "no headless managers",
    which has the same shape as a clean census.
    """
    if not os.path.isdir(directory):
        return None
    found = {}
    try:
        for path in glob.glob(os.path.join(directory, "*.json")):
            try:
                with open(path, encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError):
                continue
            sid = record.get("sessionId")
            if sid:
                found[sid] = record.get("entrypoint") or ""
    except OSError:
        return None
    return found


def defects(live, entrypoint_by_session, by_session):
    """Every `(session_id, subject)` whose live session is headless on a manager page.

    Pure: the probes are resolved by the caller, so the rule can be tested without a registry,
    a liveness endpoint or a vault.
    """
    rows = []
    for sid in sorted(live):
        if entrypoint_by_session.get(sid) != HEADLESS:
            continue
        for page in by_session.get(sid, []):
            if page["kind"] in MANAGER_KINDS:
                rows.append((sid, page["name"]))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Name every topic or goal manager running headless (exit 1 if any)."
    )
    parser.add_argument("--vault", required=True, help="the vault ROOT path")
    parser.add_argument("--sessions-dir", default=default_registry(),
                        help="the identity registry (default: $SUPERVISOR_SESSIONS_DIR or ~/.claude/sessions)")
    parser.add_argument("--live-json", help="read the live-session list from a file (tests only)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.vault):
        print(f"manager-mode census REFUSED: vault not found: {args.vault}", file=sys.stderr)
        return 2

    if args.live_json:
        try:
            with open(args.live_json, encoding="utf-8") as handle:
                live = {r["sessionId"]: r for r in json.load(handle) if r.get("sessionId")}
        except (OSError, ValueError, KeyError, TypeError) as err:
            print(f"manager-mode census REFUSED: live-session file unreadable: {err}", file=sys.stderr)
            return 2
    else:
        live = CENSUS.read_live()
        if live is None:
            print("manager-mode census REFUSED: the liveness probe could not be read", file=sys.stderr)
            return 2

    entrypoint_by_session = entrypoints(args.sessions_dir)
    if entrypoint_by_session is None:
        print(
            f"manager-mode census REFUSED: the identity registry is unreadable: {args.sessions_dir}",
            file=sys.stderr,
        )
        return 2

    _, by_session, _, _ = CENSUS.scan(args.vault)

    rows = defects(live, entrypoint_by_session, by_session)
    for sid, subject in rows:
        print(f"{sid} {subject} DEFECT manager-running-headless")

    if rows:
        print(
            f"manager-mode census: {len(rows)} headless manager(s) — each is invisible and "
            f"unreachable; see docs/fleet-surface.md § Spawn a worker item 6",
            file=sys.stderr,
        )
        return 1

    print(f"manager-mode census ok: {len(live)} live session(s), none a headless manager")
    return 0


if __name__ == "__main__":
    sys.exit(main())
