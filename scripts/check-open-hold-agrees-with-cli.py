#!/usr/bin/env python3
"""A row the open leg holds must be blocked by the CLI's own computation too.

Why this exists
---------------
`blocked_by` is written by many writers and never rewritten when its targets complete, so
the raw array drifts from the truth. Two independent readers now decide "is this row
blocked": the selection path (`scripts/open-queue.py`, through
`manager-predispatch.blocked_by_verdict`) and `vault-cli task get "<name>" blocked`. They
agree today only because both were fixed on 2026-11-15 — before that, the open leg held on
the array's *presence* while the CLI resolved the entries and returned empty.

Agreement is the property worth pinning, not either implementation. A row the open leg
reports as held while the CLI says nothing blocks it is a row the operator is told is
waiting on a dependency that is already done — measured 2026-11-15 on
`Rebuild Nuke Cluster - 2026W46-sun`, held for an hour against two `completed` blockers.

What it reads
-------------
`open-queue.py --json` for the held set (every `skipped` row whose reason is `blocked`),
and `vault-cli … task get "<name>" blocked --output json` for the other side. No registry,
no network beyond those two local reads.

⚠️ **`--fixture` is the negative control, and exit 1 there is the EXPECTED result.** A
check that only ever runs against a healthy queue cannot be distinguished from one that
cannot fire: an empty held set passes it vacuously. The fixture reconstructs the defect's
own shape — the pre-fix selection predicate holds a row whose blockers all read
`completed` while the CLI side is empty — and requires the detector to report it. Run
`--fixture` first and watch it exit 1; only then is a green live run evidence.

⚠️ **The fixture's CLI side is stated, not queried.** `vault-cli` resolves against a
registered vault, and a fixture tasks directory is not one. The premise it stands in for is
the measured one: for a row whose every blocker reads `completed`, the CLI returns an empty
`blocked` value (verified 2026-11-15 on `Rebuild Nuke Cluster - 2026W46-sun`).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
OPEN_QUEUE = os.path.join(_HERE, "open-queue.py")


def disagreements(held, blocked_of):
    """The held rows the CLI does not consider blocked.

    `held` is a list of task names; `blocked_of(name)` returns the CLI's `blocked` value
    for one — `True` when a dependency is unmet, `False` when nothing blocks it, `None`
    when the row was not in the listing. This is the whole comparison, and it is the
    function BOTH modes go through, so a fixture that fires proves the live path can.
    """
    return [name for name in held if not blocked_of(name)]


def held_by_open_leg(tasks_dir):
    """The names `open-queue.py` skipped with reason `blocked`."""
    proc = subprocess.run(
        [sys.executable, OPEN_QUEUE, "--tasks-dir", tasks_dir, "--json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise SystemExit(
            "check-open-hold: open-queue.py failed — %s"
            % (detail[-1] if detail else "exit %d" % proc.returncode)
        )
    payload = json.loads(proc.stdout)
    return [row["name"] for row in payload.get("skipped", []) if row.get("reason") == "blocked"]


def cli_blocked_map(vault):
    """`{task name: blocked}` from `vault-cli task list --all --output json`.

    ⚠️ **`task list`, never `task get <name> blocked`.** `task get` answers an unsupported
    key with an EMPTY VALUE rather than an error — `vault-cli task get <row> nosuchkey`
    returns `{"key": "nosuchkey", "value": ""}` — so a probe written against
    `task get … blocked` reads empty for every row, on every vault, forever. That is a
    probe that cannot fail, and it would report every held row as a disagreement. The
    `blocked` field is emitted by the LIST path (`pkg/ops/list.go`; pinned by
    `list_test.go`'s `"blocked":true` assertion), which is what this reads.

    A row absent from the map is reported as unreadable, never as unblocked: this check's
    failure direction is to surface a possible disagreement, not to swallow one.
    """
    proc = subprocess.run(
        ["vault-cli", "--vault", vault, "task", "list", "--all", "--output", "json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise SystemExit(
            "check-open-hold: vault-cli task list failed — %s"
            % (detail[-1] if detail else "exit %d" % proc.returncode)
        )
    payload = json.loads(proc.stdout)
    rows = payload if isinstance(payload, list) else payload.get("tasks", payload.get("items", []))
    return {row.get("name"): row.get("blocked") for row in rows if row.get("name")}


def fixture():
    """The defect's own shape, fed through `disagreements()`.

    Builds a tasks directory whose row names two blockers that both read `completed`, takes
    the held set the PRE-FIX predicate produced (`has_blocked_by`, a presence read), and
    asks the detector to catch it. Returns the names it flagged.
    """
    sys.path.insert(0, _HERE)
    import importlib.util

    spec = importlib.util.spec_from_file_location("open_queue_fixture", OPEN_QUEUE)
    open_queue = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(open_queue)

    row = "Rebuild Nuke Cluster - 2026W46-sun"
    with tempfile.TemporaryDirectory() as tasks:
        for name, status in (("Docker Registry GC (dev) - 2026W46-sat", "completed"),
                             ("Docker Registry GC (prod) - 2026W46-sat", "completed")):
            with open(os.path.join(tasks, name + ".md"), "w", encoding="utf-8") as handle:
                handle.write("---\nstatus: %s\n---\n" % status)
        with open(os.path.join(tasks, row + ".md"), "w", encoding="utf-8") as handle:
            handle.write(
                "---\nstatus: in_progress\n"
                "blocked_by:\n"
                "    - '[[Docker Registry GC (dev) - 2026W46-sat]]'\n"
                "    - '[[Docker Registry GC (prod) - 2026W46-sat]]'\n"
                "---\n"
            )
        block = open_queue.read_frontmatter(tasks, row)
        # The PRE-FIX selection predicate: presence, never resolution.
        held = [row] if open_queue.has_blocked_by(block) else []
    # The measured CLI answer for that shape — every blocker completed, so nothing blocks.
    return disagreements(held, lambda name: False)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="A row the open leg holds must be blocked by vault-cli's computation too"
    )
    parser.add_argument("--tasks-dir", help="the vault's task directory (live mode)")
    parser.add_argument("--vault", help="the vault-cli vault name (live mode)")
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="run the negative control: exit 1 when the detector fires (the expected result)",
    )
    args = parser.parse_args(argv)

    if args.fixture:
        flagged = fixture()
        if not flagged:
            print(
                "check-open-hold: FIXTURE DID NOT FIRE — the detector cannot see a held row "
                "the CLI calls unblocked, so a green live run would prove nothing.",
                file=sys.stderr,
            )
            return 0
        print(
            "check-open-hold: fixture fired on %d row(s) — the detector works.\n"
            "  ⚠️ exit 1 here is the EXPECTED result of --fixture." % len(flagged)
        )
        for name in flagged:
            print("  HELD BUT UNBLOCKED: %s" % name)
        return 1

    if not args.tasks_dir or not args.vault:
        parser.error("live mode needs --tasks-dir and --vault (or pass --fixture)")

    held = held_by_open_leg(args.tasks_dir)
    blocked_map = cli_blocked_map(args.vault)
    flagged = disagreements(held, blocked_map.get)
    if not flagged:
        print(
            "check-open-hold: %d held row(s), all blocked by the CLI's computation too"
            % len(held)
        )
        return 0
    print("check-open-hold: %d row(s) held while vault-cli reports nothing blocking them:"
          % len(flagged), file=sys.stderr)
    for name in flagged:
        why = "absent from the listing" if name not in blocked_map else "blocked=False"
        print("  %s  (%s)" % (name, why), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
