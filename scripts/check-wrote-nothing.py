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

**Two defects this guard exists to keep out, the second found in review after the first shipped.**

The first was the source of the spawn time: `metrics_sessions[].started_at` is written by
`vault-cli task work-on`, which a headless `spawn_agent` worker never runs, so every headless row
fell through and the check could not fire for the worker it exists to catch. The spawn ledger
records headless spawns and carries `spawned_at`, so it is the only source covering both.

The second was reachability: step 1's candidates are `CLASSIFICATION` rows, and
`agents/fleet-sweep-reader.md:122` states that a headless worker is *"invisible to `ListAgents`
by construction"* — so a headless worker was never a candidate in the first place. A correct
spawn-time source on an unreachable population changes nothing.

Five properties, each of which fails silently on its own:

1. **The check exists and names a class.** Without `wrote nothing since spawn` the row keeps its
   ordinary disposition and the failure is indistinguishable from an ordinary idle worker.
2. **The spawn time comes from the spawn ledger's `spawned_at`.** `claude_session_started` is a
   vault-ui Start-button flag, `nonempty 0` across every task, already refused by
   `agents/fleet-sweep-reader.md` step 5.
3. **The headless population is admitted.** Without the widened candidate set the signal fires
   only for rows the roster already showed.
4. **The comparison is in epoch seconds.** `date -r` prints local time and `spawned_at` is UTC,
   so a string comparison is off by the offset and answers for the wrong reason.
5. **The positive control is stated, and the mtime is called evidence.** A check with no opposite
   direction flags every row it cannot explain; and a task page's mtime moves when *anyone*
   writes it, so the class must not claim what the worker did.

**What this proves, and what it does not.** It proves the rule is present, uses the right source
on a reachable population, compares safely, and states both its opposite direction and its own
evidential limit. It does **not** prove a round applied it: the live evidence is a digest line
naming both timestamps.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLEET_DRIVE = REPO / "agents" / "fleet-drive.md"

#: The class name the digest must be able to print.
CLASS_MARKER = "wrote nothing since spawn"
#: The only usable spawn-time source, and the key that must NOT be used.
SPAWN_SOURCE = "`spawned_at` on the session's spawn-ledger row"
FORBIDDEN_SOURCE = "never `claude_session_started`"
#: The population half. A correct source on an unreachable set changes nothing.
HEADLESS_CANDIDATES = "a headless worker has no `classification` row"
#: The comparison half. Local time against a UTC instant answers for the wrong reason.
EPOCH_COMPARE = "epoch seconds on both sides"
#: The opposite direction, without which the check flags everything it cannot explain.
POSITIVE_CONTROL = "the worker wrote"
#: The evidential limit. An mtime moves when anyone writes the page.
EVIDENCE_NOT_PROOF = "evidence, not proof"


def main() -> int:
    failures: list[str] = []
    rel = FLEET_DRIVE.relative_to(REPO)

    if not FLEET_DRIVE.exists():
        print(f"wrote-nothing FAILED: {rel} is missing", file=sys.stderr)
        return 1

    # Case-folded: these are prose markers, and a guard that breaks on capitalisation
    # punishes a reword without telling anyone what the rule is.
    text = FLEET_DRIVE.read_text(encoding="utf-8").lower()

    def need(needle: str, why: str) -> None:
        if needle.lower() not in text:
            failures.append(f"{rel}: does not carry {needle!r}.\n      {why}")

    need(
        CLASS_MARKER,
        "Without it a worker that never wrote keeps its ordinary disposition, and the failure "
        "reads exactly like an idle worker.",
    )
    need(
        SPAWN_SOURCE,
        "A headless spawn never binds `metrics_sessions`, so that source silently skips every "
        "worker this check exists to catch. The spawn ledger records headless spawns.",
    )
    need(
        FORBIDDEN_SOURCE,
        "`claude_session_started` is a vault-ui Start-button flag, nonempty on zero tasks — a "
        "reader who reaches for it gets a check that can never fire.",
    )
    need(
        HEADLESS_CANDIDATES,
        "Step 1's candidates are `CLASSIFICATION` rows and a headless worker has none "
        "(`agents/fleet-sweep-reader.md:122`), so a correct spawn time on that population still "
        "never runs. The candidate set must be widened to the ledger.",
    )
    need(
        EPOCH_COMPARE,
        "`date -r` prints local time and `spawned_at` is UTC; a string comparison is off by the "
        "offset and answers for the wrong reason.",
    )
    need(
        POSITIVE_CONTROL,
        "A check with no opposite direction flags every row it cannot explain, which is worse "
        "than not having it.",
    )
    need(
        EVIDENCE_NOT_PROOF,
        "A task page's mtime moves when anyone writes it, so the class must not claim what the "
        "worker did — it asserts the page was not modified, and says so.",
    )

    if failures:
        print(f"wrote-nothing FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        "wrote-nothing ok: the drive leg names the class, takes the spawn time from the spawn "
        "ledger's `spawned_at`, refuses claude_session_started, admits the headless population, "
        "compares in epoch seconds, and states both its positive control and its evidential limit"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
