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

Three properties, each of which fails silently on its own:

1. **The check exists and names a class.** Without `wrote nothing since spawn` the row keeps its
   ordinary disposition and the failure is indistinguishable from an ordinary idle worker.
2. **The spawn time comes from `metrics_sessions[].started_at`.** `claude_session_started` is a
   vault-ui Start-button flag, `nonempty 0` across every task in the payload profiler, and the
   orphan seed already refuses it (`agents/fleet-sweep-reader.md` step 5). Reading the wrong key
   gives a timestamp that is never present, so every row silently falls through.
3. **The positive control is stated.** A check with no opposite direction flags every row it
   cannot explain, which is worse than not having it — a worker that did write must keep its
   ordinary disposition.

**What this proves, and what it does not.** It proves the rule is present, uses the right key and
states its opposite direction. It does **not** prove a round applied it: the live evidence is a
digest line naming both timestamps.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLEET_DRIVE = REPO / "agents" / "fleet-drive.md"

#: The class name the digest must be able to print.
CLASS_MARKER = "wrote nothing since spawn"
#: The only usable spawn-time source, and the key that must NOT be used.
SPAWN_SOURCE = "metrics_sessions[].started_at"
FORBIDDEN_SOURCE = "never `claude_session_started`"
#: The opposite direction, without which the check flags everything it cannot explain.
POSITIVE_CONTROL = "the worker wrote"


def main() -> int:
    failures: list[str] = []
    rel = FLEET_DRIVE.relative_to(REPO)

    if not FLEET_DRIVE.exists():
        print(f"wrote-nothing FAILED: {rel} is missing", file=sys.stderr)
        return 1

    # Case-folded: these are prose markers, and a guard that breaks on capitalisation
    # punishes a reword without telling anyone what the rule is.
    text = FLEET_DRIVE.read_text(encoding="utf-8").lower()

    if CLASS_MARKER.lower() not in text:
        failures.append(
            f"{rel}: carries no {CLASS_MARKER!r} class.\n"
            f"      Without it a worker that never wrote keeps its ordinary disposition, and the\n"
            f"      failure reads exactly like an idle worker."
        )
    if SPAWN_SOURCE.lower() not in text:
        failures.append(
            f"{rel}: does not name {SPAWN_SOURCE!r} as the spawn time.\n"
            f"      It is the only key that is actually written; anything else gives a timestamp\n"
            f"      that is never present and every row silently falls through."
        )
    if FORBIDDEN_SOURCE.lower() not in text:
        failures.append(
            f"{rel}: does not refuse `claude_session_started`.\n"
            f"      It is a vault-ui Start-button flag, nonempty on zero tasks — a reader who\n"
            f"      reaches for it gets a check that can never fire."
        )
    if POSITIVE_CONTROL.lower() not in text:
        failures.append(
            f"{rel}: states no positive control.\n"
            f"      A check with no opposite direction flags every row it cannot explain, which is\n"
            f"      worse than not having it."
        )

    if failures:
        print(f"wrote-nothing FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        "wrote-nothing ok: the drive leg names the class, takes the spawn time from "
        f"{SPAWN_SOURCE}, refuses claude_session_started, and states its positive control"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
