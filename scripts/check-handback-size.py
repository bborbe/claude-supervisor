#!/usr/bin/env python3
"""The hand-back contract's ≤ 1 KB cap, asserted rather than described.

`docs/handback-contract.md` § The shape says a leg returns **≤ 1 KB** and the detail goes to
the payload. Nothing measured it, so every leg could exceed the cap on every round and no
check would fail — the same "a rule that lives only in prose" defect `check-handback-keys.py`
catches one layer down for *keys*. This asserts the size.

**What is measured, and what it does not prove.** The agent's own `<output_format>` hand-back
fence, minus the lines the contract declares **variable** (the truncation exception:
`crash-loop:` / `cannot-tell:` / `park-nudge:` rows, `refused:` lines). Those are unbounded by
design — the payload carries the remainder — so counting them would fail a leg that is behaving
correctly.

⚠️ **This pins the authored shape, not a live round.** A placeholder is not always longer than
its value: `<n>` and `<pct>` are, but `<path>` and `<id>` stand in for strings that can exceed
them, so a template under the cap does not prove every instance is. What it does prove is that
the *fixed* text — the keys, the separators, the labels a leg controls — leaves room for the
values, and a leg that overruns the cap on a real round has broken its own contract, which is a
review question rather than something a static check can see.
"""

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_BYTES = 1024

#: The lines the contract declares variable per leg — never counted against the cap. Keys are
#: the leg names `check-handback-keys.LEGS` uses. A leg absent here has no variable lines and
#: its whole hand-back must fit.
VARIABLE = {
    "supervisor:fleet-keepalive-actor": ("crash-loop:", "cannot-tell:", "park-nudge:"),
    "supervisor:fleet-open-actor": ("refused:",),
}


def load_legs():
    """`check-handback-keys.LEGS` — one registry, so a new leg is guarded by both checks."""
    path = REPO / "scripts" / "check-handback-keys.py"
    spec = importlib.util.spec_from_file_location("check_handback_keys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LEGS


def handback_fence(text: str) -> str | None:
    """Every fenced block under `**Hand-back**`, joined, or None when there is not one.

    ⚠️ **All fences, not the first.** A leg may put its counts in one block and its
    per-row lines in a second (the open leg's `refused:` rows are exactly that), so reading
    only the first would leave that leg's declared variable lines uncounted and its
    exclusion inert — the check would report a pass for a measurement it never took.
    """
    _, sep, rest = text.partition("**Hand-back**")
    if not sep:
        return None
    body = rest.partition("</output_format>")[0]
    blocks = re.findall(r"```[a-z]*\n(.*?)```", body, re.DOTALL)
    return "\n".join(blocks) if blocks else None


def fixed_bytes(fence: str, variable: tuple[str, ...]) -> int:
    """Byte length of the fence minus its declared variable lines."""
    kept = [ln for ln in fence.splitlines()
            if not any(ln.lstrip().startswith(p) for p in variable)]
    return len("\n".join(kept).rstrip("\n").encode("utf-8"))


def main(root: Path | None = None) -> int:
    root = Path(root) if root is not None else REPO
    legs = load_legs()
    failures: list[str] = []
    readings: list[str] = []
    for name, leg in sorted(legs.items()):
        agent = root / leg["agent"]
        if not agent.exists():
            failures.append(f"{leg['agent']}: missing — {name}'s hand-back cannot be measured")
            continue
        fence = handback_fence(agent.read_text(encoding="utf-8"))
        if fence is None:
            failures.append(f"{leg['agent']}: no `**Hand-back**` fenced block — nothing to measure")
            continue
        size = fixed_bytes(fence, VARIABLE.get(name, ()))
        readings.append(f"{name}={size}B")
        if size > MAX_BYTES:
            failures.append(
                f"{leg['agent']}: hand-back is {size} B over its {MAX_BYTES} B cap "
                f"(fixed lines only) — move detail to the payload, or declare the variable "
                f"lines in this script's VARIABLE map"
            )
    if failures:
        print(f"handback-size FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"handback-size ok: {len(readings)} leg(s) under {MAX_BYTES} B — {', '.join(readings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
