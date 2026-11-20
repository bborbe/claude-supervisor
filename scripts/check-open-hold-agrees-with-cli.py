#!/usr/bin/env python3
"""A row `open-queue.py` holds must be blocked by the CLI's own computation too.

Why this exists
---------------
`blocked_by` is written by many writers and never rewritten when its targets complete, so
the raw array drifts from the truth. Two independent readers now decide "is this row
blocked": the selection path (`scripts/open-queue.py`, through
`manager-predispatch.blocked_by_verdict`) and `vault-cli task list`'s `blocked` field. They
agree today only because both were fixed on 2026-11-15 — before that, `open-queue.py` held on
the array's *presence* while the CLI resolved the entries and returned `false`.

Agreement is the property worth pinning, not either implementation. A row `open-queue.py`
reports as held while the CLI says nothing blocks it is a row the operator is told is
waiting on a dependency that is already done — measured 2026-11-15 on
`Rebuild Nuke Cluster - 2026W46-sun`, held for an hour against two `completed` blockers.

What it reads
-------------
`open-queue.py --json` for the held set (every `skipped` row whose reason is `blocked`),
and `vault-cli --vault <v> task list --all --output json` for the other side. No registry,
no network beyond those two local reads.

⚠️ **`task list`, never `task get <name> blocked`.** `task get` answers an unsupported key
with an EMPTY VALUE rather than an error — `vault-cli task get <row> nosuchkey` returns
`{"key": "nosuchkey", "value": ""}`, the same shape as a real empty answer. A probe written
against `task get … blocked` therefore reads empty for every row, on every vault, forever:
a check that can never fail, which would report every held row as a disagreement. `blocked`
is emitted by the LIST path (`pkg/ops/list.go`; pinned by `list_test.go`'s `"blocked":true`
assertion).

⚠️ **`--fixture` is a negative control, and exit 1 there is the EXPECTED result — never
wire it into CI.** A check that only ever runs against a healthy queue cannot be
distinguished from one that cannot fire: an empty held set passes it vacuously. The fixture
builds the defect's own shape in a temp tasks directory and asserts BOTH directions:

  1. the CURRENT `open-queue.py` does not hold the row (the fix works — a regression here
     exits 2, which is a real failure);
  2. the detector reports that row when it is handed the pre-fix held set (the check can
     fire — exit 1, the expected result).

The fixture's CLI side is stated, not queried: `vault-cli` resolves against a registered
vault, and a fixture tasks directory is not one. The premise it stands in for is the
measured one — for a row whose every blocker reads `completed`, the CLI returns
`blocked: false` (verified 2026-11-15 on `Rebuild Nuke Cluster - 2026W46-sun`).
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
OPEN_QUEUE = os.path.join(_HERE, "open-queue.py")

#: A hung `vault-cli` or `open-queue.py` must fail the check, never hang the caller.
READ_TIMEOUT_SECONDS = 600


def disagreements(held, blocked_of):
    """The held rows the CLI does not consider blocked.

    `held` is a list of task names; `blocked_of(name)` returns the CLI's `blocked` value
    for one — `True` when a dependency is unmet, `False` when nothing blocks it, `None`
    when the row was not in the listing. This is the whole comparison, and it is the
    function BOTH modes go through, so a fixture that fires proves the live path can.
    """
    return [name for name in held if not blocked_of(name)]


def false_admits(admitted, blocked_of):
    """The admitted rows the CLI DOES consider blocked — the other direction.

    A one-directional check cannot tell a correct pair of implementations from two that
    both say "not blocked": it only ever asks whether a held row is really blocked. This
    asks the converse — a row the leg opens while the CLI reports an unmet dependency is
    the same defect mirrored, and it is the one that spawns a worker onto blocked work.
    """
    return [name for name in admitted if blocked_of(name)]


def open_leg_sets(tasks_dir):
    """`(held, admitted)` — the names `open-queue.py` skipped as `blocked`, and selected.

    ⚠️ **The JSON shape is pinned, not probed.** `open-queue.py --json` emits an object
    with `selected` and `skipped` arrays; a `.get(…, [])` default would turn any shape
    change into two empty sets, and both directions below would then pass vacuously —
    the exact failure this check exists to catch, one layer down. Refuse the shape.
    """
    try:
        proc = subprocess.run(
            [sys.executable, OPEN_QUEUE, "--tasks-dir", tasks_dir, "--json"],
            capture_output=True,
            text=True,
            timeout=READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("check-open-hold: open-queue.py timed out after %ds" % READ_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise SystemExit(
            "check-open-hold: open-queue.py failed — %s"
            % (detail[-1] if detail else "exit %d" % proc.returncode)
        )
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise SystemExit("check-open-hold: open-queue.py --json is not JSON — %s" % exc)
    if not isinstance(payload, dict) or not isinstance(payload.get("selected"), list) \
            or not isinstance(payload.get("skipped"), list):
        raise SystemExit(
            "check-open-hold: open-queue.py --json did not carry `selected` and `skipped` "
            "lists — the read shape changed and this check's comparison would be empty"
        )
    held = [row["name"] for row in payload["skipped"] if row.get("reason") == "blocked"]
    admitted = [row["name"] for row in payload["selected"]]
    return held, admitted


def cli_blocked_map(vault):
    """`{task name: blocked}` from `vault-cli task list --all --output json`.

    ⚠️ **The top-level shape is pinned to a LIST.** `task list --output json` emits a JSON
    array; a dict-shaped guess (`payload.get("tasks", …)`) would yield an EMPTY map on any
    shape change, and every held row would then read "absent from the listing" — a loud
    failure, but one that names the wrong cause. Refuse the shape instead of guessing it.

    A row absent from the map is reported as absent, never as unblocked: this check's
    failure direction is to surface a possible disagreement, not to swallow one.
    """
    try:
        proc = subprocess.run(
            ["vault-cli", "--vault", vault, "task", "list", "--all", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("check-open-hold: vault-cli task list timed out after %ds" % READ_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise SystemExit(
            "check-open-hold: vault-cli task list failed — %s"
            % (detail[-1] if detail else "exit %d" % proc.returncode)
        )
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise SystemExit("check-open-hold: vault-cli task list --output json is not JSON — %s" % exc)
    if not isinstance(payload, list):
        raise SystemExit(
            "check-open-hold: vault-cli task list --output json returned %s, expected a list "
            "— the read shape changed and this check's comparison would be empty" % type(payload).__name__
        )
    return {row.get("name"): row.get("blocked") for row in payload if row.get("name")}


def fixture():
    """Both directions of the control, from the defect's own shape.

    Builds a tasks directory whose row names two blockers that both read `completed`, then
    returns `(held_now, flagged)`: what the CURRENT selection path holds, and what the
    detector reports when handed the PRE-FIX held set.
    """
    spec = importlib.util.spec_from_file_location("open_queue_fixture", OPEN_QUEUE)
    open_queue = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(open_queue)

    row = "Rebuild Nuke Cluster - 2026W46-sun"
    with tempfile.TemporaryDirectory() as tasks:
        for name in ("Docker Registry GC (dev) - 2026W46-sat",
                     "Docker Registry GC (prod) - 2026W46-sat"):
            with open(os.path.join(tasks, name + ".md"), "w", encoding="utf-8") as handle:
                handle.write("---\nstatus: completed\n---\n")
        # ⚠️ **`approved_at` AND `flag` are both load-bearing, not decoration.**
        # `open-queue.py` does not read the task directory itself — it calls
        # `approved-not-started.py` (predicate: `approved_at`, or an operator-set flag),
        # and then `select()` admits only rows whose flag is truthy. A fixture row
        # carrying neither never enters the payload, so `guards()` never sees it and the
        # regression direction below passes VACUOUSLY: it would report "the current code
        # does not hold it" for a row the queue never considered. Caught in review
        # 2026-11-15 on this very script — hence the explicit vacuity check in `main()`.
        with open(os.path.join(tasks, row + ".md"), "w", encoding="utf-8") as handle:
            handle.write(
                "---\nstatus: in_progress\n"
                "approved_at: '2026-11-15T09:00:00+00:00'\n"
                "flag: true\nflag_set_by: operator\n"
                "blocked_by:\n"
                "    - '[[Docker Registry GC (dev) - 2026W46-sat]]'\n"
                "    - '[[Docker Registry GC (prod) - 2026W46-sat]]'\n"
                "---\n"
            )
        held_now, admitted_now = open_leg_sets(tasks)
        block = open_queue.read_frontmatter(tasks, row)
        # The PRE-FIX selection predicate: presence, never resolution.
        held_before = [row] if open_queue.has_blocked_by(block) else []
    # The measured CLI answer for that shape — every blocker completed, so nothing blocks.
    return held_now, admitted_now, row, disagreements(held_before, lambda name: False)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="A row `open-queue.py` holds must be blocked by vault-cli's computation too"
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
        held_now, admitted_now, row, flagged = fixture()
        if held_now:
            print(
                "check-open-hold: REGRESSION — the current selection path still holds %s, "
                "whose blockers all read `completed`." % ", ".join(held_now),
                file=sys.stderr,
            )
            return 2
        if row not in admitted_now:
            print(
                "check-open-hold: FIXTURE IS VACUOUS — %s never reached the admitted set, so "
                "`not held` says nothing about the guard. Check the row still satisfies "
                "`approved-not-started.py`'s predicate (approved_at, or an operator-set flag)."
                % row,
                file=sys.stderr,
            )
            return 2
        if not flagged:
            print(
                "check-open-hold: FIXTURE DID NOT FIRE — the detector cannot see a held row "
                "the CLI calls unblocked, so a green live run would prove nothing.",
                file=sys.stderr,
            )
            return 2
        print(
            "check-open-hold: fixture ok — %s is admitted by open-queue.py, and the "
            "detector fires on %d row(s) of the pre-fix shape.\n"
            "  ⚠️ exit 1 here is the EXPECTED result of --fixture; never wire this into CI."
            % (row, len(flagged))
        )
        for name in flagged:
            print("  HELD BUT UNBLOCKED: %s" % name)
        return 1

    if not args.tasks_dir or not args.vault:
        parser.error("live mode needs --tasks-dir and --vault (or pass --fixture)")

    held, admitted = open_leg_sets(args.tasks_dir)
    blocked_map = cli_blocked_map(args.vault)
    wrongly_held = disagreements(held, blocked_map.get)
    wrongly_opened = false_admits(admitted, blocked_map.get)
    if not wrongly_held and not wrongly_opened:
        print(
            "check-open-hold: %d held and %d admitted row(s); both directions agree with "
            "the CLI's computation" % (len(held), len(admitted))
        )
        return 0
    if wrongly_held:
        print("check-open-hold: %d row(s) held while vault-cli reports nothing blocking them:"
              % len(wrongly_held), file=sys.stderr)
        for name in wrongly_held:
            why = "absent from the listing" if name not in blocked_map else "blocked=false"
            print("  %s  (%s)" % (name, why), file=sys.stderr)
    if wrongly_opened:
        print("check-open-hold: %d row(s) admitted while vault-cli reports an unmet blocker:"
              % len(wrongly_opened), file=sys.stderr)
        for name in wrongly_opened:
            print("  %s  (blocked=true)" % name, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
