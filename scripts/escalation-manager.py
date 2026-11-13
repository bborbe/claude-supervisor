#!/usr/bin/env python3
"""escalation-manager — the manager that owns one task, for a blocked `ESCALATION` row.

WHY THIS EXISTS. `agents/fleet-drive.md` renders an `ESCALATION` row for every blocked
session, but the row carried only a jump link: a session whose closer named no owner was
escalated to nobody in particular. The rule that answers it already exists —
`docs/fleet-surface.md` § *Which manager owns a subject* — but it was written as prose
naming two Python functions, and a reader cannot call prose. Three review rounds on that
prose produced three different defects, the last of them fatal: the drive leg is handed a
prompt, not a board, so "read the parent the board already computed" was an instruction
with nothing to read.

So the resolution is a script. It is a script rather than a snippet because the rule
regressed twice as prose and prose has nothing to run against it —
`scripts/tests/test_escalation_manager.py` pins the cases.

⚠️ **It reuses `fleet-board.py`'s grouping; it does not re-derive the tiers.** Tier 3 —
"the goal itself before the topic whose `## Goals` lists it" — is `manager_on_chain`'s
job, and a second implementation here is the drift `agents/fleet-drive.md` forbids
elsewhere. The module is loaded by path because the filename carries a hyphen.

⚠️ **A failed read exits non-zero. It never renders `fleet`.** That fallback is tier 4's
answer for a subject with no live ancestor, and it is the WRONG answer for a board that
could not be read: `fleet` on an unreadable board silently claims the Fleet Manager owns
a row it may not own, which is the `A Wait Needs a Performer` defect this field exists to
close. `cannot-tell` and `nobody` must never render the same.

Usage:
    escalation-manager.py "<task title>"          # the manager's session name, or `fleet`
    escalation-manager.py --json "<task title>"   # {"manager": ..., "source": ...}

Exit 0 with a name (including `fleet`), non-zero when the read failed.
"""

import argparse
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
FLEET_MANAGER = "fleet"


def _fleet_board():
    """`scripts/fleet-board.py`, loaded by path — the filename carries a hyphen."""
    path = os.path.join(_HERE, "fleet-board.py")
    spec = importlib.util.spec_from_file_location("fleet_board", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fleet_sessions():
    """`scripts/fleet-sessions.py`, loaded by path — the filename carries a hyphen."""
    path = os.path.join(_HERE, "fleet-sessions.py")
    spec = importlib.util.spec_from_file_location("fleet_sessions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_many(task_titles, board=None):
    """`{title: (manager_name, source)}` — the grouping built ONCE for every title.

    ⚠️ **This exists because the grouping costs minutes, not milliseconds.** Measured
    2026-11-12: one resolution took **387 s** (`vault_index` 64 s, `colour_census` 37 s,
    `build_task_index` 87 s, under load). A caller resolving per row pays that per row,
    which is the defect `round-detectors.py` batches away. One build, N lookups —
    `manager_on_chain` itself measured 0.0 s.

    `source` is `chain` when a live manager resolved through the tiers, or `fleet` for
    tier 4's fallback. ⚠️ Raises rather than returning `fleet` when the inputs cannot be
    read — see the module docstring. A repeated title is looked up once.
    """
    fb = board or _fleet_board()
    registry = fb.registry_records()
    if registry is None:
        raise RuntimeError("session registry unreadable")
    index = fb.vault_index()
    # ⚠️ `build_grouping` wants `sid -> [task titles]`, built by `fleet-sessions.py` —
    # the same two calls `fleet-board.py`'s own `manager_sessions()` makes. A bare title
    # list raises inside the grouping, so this is the one construction, not a guess.
    # ⚠️ The grouping wants the `sid -> [titles]` MAP; the loop below wants the caller's
    # list. Two names, because iterating the map's keys silently resolves session ids.
    stamps, _ = _fleet_sessions().build_task_index()
    grouping = fb.build_grouping(
        registry, index, fb.colour_census(), fb.loop_slugs(), stamps)
    out = {}
    for title in dict.fromkeys(task_titles):
        manager = fb.manager_on_chain(
            index.goals_of(title), index, grouping.roles, grouping.subjects,
            index.managed_by_of(title), task_title=title)
        if not manager:
            out[title] = (FLEET_MANAGER, "fleet")
            continue
        name = (registry.get(manager) or {}).get("name") or ""
        name = name.strip()
        if not name:
            raise RuntimeError(f"manager session {manager} carries no registry name")
        out[title] = (name, "chain")
    return out


def resolve(task_title, board=None):
    """`(manager_name, source)` for one title — the same resolution, one title."""
    return resolve_many([task_title], board=board)[task_title]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_title")
    parser.add_argument("--json", action="store_true", help="emit {manager, source}")
    args = parser.parse_args(argv)
    try:
        manager, source = resolve(args.task_title)
    except Exception as exc:  # fail loudly, never as `fleet`
        print(f"escalation-manager: {exc} — the manager is UNKNOWN, not `fleet`",
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"manager": manager, "source": source}, sort_keys=True))
    else:
        print(manager)
    return 0


if __name__ == "__main__":
    sys.exit(main())
