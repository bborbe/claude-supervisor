#!/usr/bin/env python3
"""A worker that exits having written nothing is reported as a failure, not as finished.

The fleet round's drive leg revives `parked` candidates and reaps `finished` rows, and both
assume a worker that did something. Neither could tell a worker that never wrote from one that
wrote and stopped: `finished` is decided by the task's own boxes (`agents/fleet-sweep-reader.md`
step 8 — `idle` and every box `[x]`), which a task completed before the worker spawned satisfies
without the worker having done anything.

Measured 2026-11-07: a fleet manager's worker hit a plan gate, nobody answered, the 15-minute
timeout denied it, and the worker exited with its task file untouched — found twenty minutes
later. The round had no row for it: not finished (its boxes were open), not stalled (its turn had
ended), and a nudge cannot reach a worker that never started.

**Three defects this guard exists to keep out, each found only after the one before it shipped.**

The first was the spawn-time source: `metrics_sessions[].started_at` is written by
`vault-cli task work-on`, which a headless `spawn_agent` worker never runs, so every headless row
fell through. The second was reachability: step 1's candidates are `CLASSIFICATION` rows, and
`agents/fleet-sweep-reader.md:122` states a headless worker is *"invisible to `ListAgents` by
construction"*, so it was never a candidate at all. The third was executability: the replacement
read `python3 $P/spawn-ledger.py` (a library module — it printed nothing and exited 0) and
`date -r <file> +%s` (GNU-only, errors on the fleet's macOS host), so the corrected rule still
could not run.

Nine properties, each of which fails silently on its own:

1. **The check exists and names a class.**
2. **The spawn time comes from the spawn ledger's `spawned_at`**, never `claude_session_started`
   (a vault-ui flag, `nonempty 0` on every task).
3. **The headless population is admitted** — a correct source on an unreachable set changes nothing.
4. **The mtime read is portable** (`os.path.getmtime`), not a `date -r` call.
5. **The ledger read is executable** (`--json`), not a bare module invocation.
6. **The join key is named** — the ledger carries a full uuid, `CLASSIFICATION` an 8-char handle.
7. **A grace window exists** — otherwise a worker spawned seconds ago is flagged immediately.
8. **The verdict is wired** into step 5's table and the hand-back counts, or a row it classifies
   has no ledger value and no count key.
9. **The positive control is stated, and the mtime is called evidence.**

**What this proves, and what it does not.** It proves the rule is present, uses executable and
portable commands on a reachable population, states both its opposite direction and its own
evidential limit, and is wired into the round's output contract. It does **not** prove a round
applied it: the live evidence is a digest line naming both timestamps.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLEET_DRIVE = REPO / "agents" / "fleet-drive.md"

#: (marker, why). Every marker is prose, matched case-folded — a guard that breaks on
#: capitalisation punishes a reword without telling anyone what the rule is.
REQUIRED = [
    (
        "wrote nothing since spawn",
        "Without the class name a worker that never wrote keeps its ordinary disposition, and "
        "the failure reads exactly like an idle worker.",
    ),
    (
        "the spawn time is `spawned_at`",
        "A headless spawn never binds `metrics_sessions`, so that source silently skips every "
        "worker this check exists to catch. The spawn ledger records headless spawns.",
    ),
    (
        "never `claude_session_started`",
        "`claude_session_started` is a vault-ui Start-button flag, nonempty on zero tasks — a "
        "reader who reaches for it gets a check that can never fire.",
    ),
    (
        "a headless worker has no `classification` row",
        "Step 1's candidates are `CLASSIFICATION` rows and a headless worker has none "
        "(`agents/fleet-sweep-reader.md:122`), so a correct spawn time on that population still "
        "never runs.",
    ),
    (
        "os.path.getmtime",
        "`date -r <file> +%s` is GNU-only and errors on the fleet's macOS host; the portable "
        "idiom is `os.path.getmtime`, as `agents/goal-sweep-reader.md:95` already uses.",
    ),
    (
        "spawn-ledger.py --json",
        "`spawn-ledger.py` is a library module: run bare it prints nothing and exits 0, so a "
        "caller reads empty stdout as 'no rows'. The `--json` CLI is what makes it executable.",
    ),
    (
        "join on the first 8 characters",
        "The ledger keys on the full uuid while `CLASSIFICATION` carries an 8-char handle; an "
        "unnamed join matches nothing or, worse, duplicates rows.",
    ),
    (
        "15-minute park window",
        "Without a grace window `mtime <= spawned_at` holds from the first second for any task a "
        "manager wrote before spawning, so a worker spawned moments ago is flagged at once.",
    ),
    (
        "| **wrote-nothing** |",
        "Step 5's verdict table is a closed set; a class missing from it has no ledger value and "
        "no count key, so `candidates=N` cannot reconcile.",
    ),
    (
        "wrote-nothing=<W>",
        "The hand-back's counts line must carry the new verdict, or the caller cannot read it.",
    ),
    (
        "the worker wrote",
        "A check with no opposite direction flags every row it cannot explain, which is worse "
        "than not having it.",
    ),
    (
        "evidence, not proof",
        "A task page's mtime moves when anyone writes it, so the class must not claim what the "
        "worker did — it asserts the page was not modified, and says so.",
    ),
]


def main() -> int:
    if not FLEET_DRIVE.exists():
        print(f"wrote-nothing FAILED: {FLEET_DRIVE.relative_to(REPO)} is missing", file=sys.stderr)
        return 1

    rel = FLEET_DRIVE.relative_to(REPO)
    text = FLEET_DRIVE.read_text(encoding="utf-8").lower()

    failures = [
        f"{rel}: does not carry {marker!r}.\n      {why}"
        for marker, why in REQUIRED
        if marker.lower() not in text
    ]

    if failures:
        print(f"wrote-nothing FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        "wrote-nothing ok: the drive leg names the class, takes the spawn time from the spawn "
        "ledger's `spawned_at`, refuses claude_session_started, admits the headless population, "
        "reads mtime and the ledger with executable commands, names the join key, honours a grace "
        "window, wires the verdict into the round's counts, and states both its positive control "
        "and its evidential limit"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
