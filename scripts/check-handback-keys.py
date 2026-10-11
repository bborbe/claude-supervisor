#!/usr/bin/env python3
"""Every key a router `When` cell tests must be emitted by the sweep hand-back.

The fleet tick's router gates a step on a hand-back **count** — `the hand-back reports
`blocked` > 0`. A key the sweep reader never emits makes that cell false forever, so the
step silently never runs. That is the same defect class `check-step-when.py` catches for
a drifted `When:` line, one layer down: there the trigger and its body disagree, here the
trigger and the data it tests do.

It is not hypothetical. The hand-back was introduced with the class `held` present in the
digest render and absent from the key set, and `held` is the class the render treats as
winning over every other — so the one bucket a reader most needed was the one no cell
could see. This asserts the whole set instead of the one key that happened to be caught.

Reads both artifacts directly: no registry, no build, no network.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROUTER = REPO / "commands" / "fleet-tick.md"
SWEEP = REPO / "agents" / "fleet-sweep-reader.md"

# `the hand-back reports `orphan`, `collision` or `unmanaged` > 0` — the count-test cell shape.
CELL = re.compile(r"hand-back reports\s+((?:`[a-z0-9_]+`(?:\s*,\s*|\s+or\s+)?)+?)\s*>\s*0")
KEY = re.compile(r"`([a-z0-9_]+)`")


def keys_tested(text: str) -> set[str]:
    found: set[str] = set()
    for m in CELL.finditer(text):
        found.update(KEY.findall(m.group(1)))
    return found


def keys_emitted(text: str) -> set[str]:
    """Keys in the `<output_format>` hand-back block, which is where the required set lives."""
    _, sep, rest = text.partition("**Hand-back**")
    if not sep:
        return set()
    body = rest.partition("</output_format>")[0]
    return set(re.findall(r"\b([a-z0-9_]+)=", body))


def main() -> int:
    tested = keys_tested(ROUTER.read_text(encoding="utf-8"))
    emitted = keys_emitted(SWEEP.read_text(encoding="utf-8"))

    if not tested:
        print(
            "handback-keys FAILED: no count-test When cell found in the router — either the "
            "cell shape changed or the router lost its hand-back cells",
            file=sys.stderr,
        )
        return 1

    missing = sorted(tested - emitted)
    if missing:
        print(
            f"handback-keys FAILED: {len(missing)} key(s) tested by a router When cell but never "
            f"emitted by the sweep hand-back",
            file=sys.stderr,
        )
        for k in missing:
            print(f"  - `{k}` is never emitted, so its cell is false forever", file=sys.stderr)
        return 1

    print(f"handback-keys ok: {len(tested)} tested key(s), all emitted by the sweep hand-back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
