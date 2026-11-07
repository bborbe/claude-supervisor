#!/usr/bin/env python3
"""Render one parked-worker lever row, and refuse a lever that names no lever.

    parked-lever.py --pane <id|-> --waits "<what it waits on>" \
                    --lever "<the one act that clears it>" --owner <owner>

Prints one line:

    <jump link> · waits: <...> · lever: <...> · owner: <...>

WHY THIS IS A SCRIPT AND NOT PROSE. A round that names a lever per parked worker
turns "read every pane" into a scan only if the lever is a specific act with an
owner. "the fleet" and "CI" are the two strings a round reaches for when it does
not know, and they read exactly like an answer — both name something no one in
the round can act on, so the operator still has to open the pane. The vocabulary
is therefore checked here, not described in a step file.

THE TAXONOMY — three owners, nothing else:

  manager            the round clears it itself (nudge, re-arm a watcher, reap,
                     resume, ready a row). It must then do so in the same round
                     and report the act as done; it is never offered back.
  operator           only the operator can clear it (a `todo` approval, an
                     irreversible or production-touching action, a decision).
  machine:<watcher>  a named watcher clears it when it fires. The name is what
                     lets the operator tell "a cron will pick this up" from
                     "nothing will".

A lever is refused when it names none of these: a party with no act, a system
that is not a named watcher, or a restatement of the wait itself. Refusal is
exit 2 with the reason on stderr, so a round that cannot name a lever reports
the gap instead of printing a row that looks answered.

A HEADLESS WORKER HAS NO PANE. `--pane -` renders `no pane (headless)` rather
than a dead jump target: a link that cannot be followed is worse than none.

BUT A TAB WORKER ALWAYS HAS ONE. `--pane ?` renders `pane unresolved (tab
worker)`. The first live sample passed `-` for a tab worker whose pane id had
not been resolved, and the row told the operator there was no window to open —
the one direction this field must never get wrong.

EXIT CODES. 0 rendered · 2 usage or validation error.
"""

import argparse
import subprocess
import sys
from pathlib import Path

FIELD_SEP = " · "

OWNER_KINDS = ("manager", "operator")

# Names that read as a lever and are not one: a party with no act, a system
# that is not a named watcher, or the wait restated.
REJECTED_LEVERS = frozenset({
    "the fleet", "fleet", "the team", "team",
    "ci", "the ci", "the pipeline", "pipeline",
    "someone", "somebody", "anyone", "nobody", "them", "they",
    "wait", "waiting", "time", "eventually", "later", "soon", "unknown", "tbd",
})


class LeverError(ValueError):
    """A row that cannot be rendered honestly."""


def _norm(raw: str) -> str:
    return " ".join(raw.strip().lower().split())


def validate_waits(raw: str) -> str:
    waits = " ".join(raw.strip().split())
    if not waits:
        raise LeverError("waits is empty — say what the worker is parked on")
    return waits


def validate_lever(raw: str) -> str:
    lever = " ".join(raw.strip().split())
    if not lever:
        raise LeverError("lever is empty — name the one act that clears the wait")
    if _norm(lever) in REJECTED_LEVERS:
        raise LeverError(
            f"{lever!r} names no lever — it is a party or a system, not an act with an "
            "owner. Name what clears the wait: a manager act, an operator decision, or "
            "machine: <watcher>."
        )
    return lever


def validate_owner(raw: str) -> str:
    owner = " ".join(raw.strip().split())
    if not owner:
        raise LeverError("owner is empty — one of manager, operator, machine: <watcher>")
    head = _norm(owner)
    if head in OWNER_KINDS:
        return head
    if head.startswith("machine:"):
        watcher = owner.split(":", 1)[1].strip()
        if not watcher:
            raise LeverError("machine: carries no watcher name — say which watcher fires")
        if _norm(watcher) in REJECTED_LEVERS:
            raise LeverError(
                f"{watcher!r} is not a watcher — name the script or job that fires"
            )
        return f"machine: {watcher}"
    raise LeverError(
        f"{owner!r} is not an owner — use manager, operator, or machine: <watcher>"
    )


def jump_link(pane: str) -> str:
    """The operator's way to reach the pane, degrading to the command form.

    Three states, and the third is the one the first live sample got wrong: a
    headless worker has no pane at all, while a TAB worker whose pane id was not
    resolved does have one. Collapsing the second into the first tells the
    operator to stop looking for a window that exists. `-` is headless, `?` is
    unresolved.
    """
    p = (pane or "").strip()
    if p == "-":
        return "no pane (headless)"
    if p == "?":
        return "pane unresolved (tab worker — resolve before reporting)"
    if not p:
        raise LeverError("pane is empty — pass a pane id, - for headless, or ? for unresolved")
    script = Path(__file__).resolve().parent / "jump-link.py"
    try:
        out = subprocess.run(
            [sys.executable, str(script), pane.strip()],
            capture_output=True, text=True, timeout=10,
        )
        line = out.stdout.strip()
        if out.returncode == 0 and line:
            return line
    except (OSError, subprocess.SubprocessError):
        pass
    return f"/supervisor:jump {pane.strip()}"


def render(pane: str, waits: str, lever: str, owner: str) -> str:
    # Validate before resolving the link: a refused row must not spawn a
    # subprocess for a jump target it is never going to print.
    waits = validate_waits(waits)
    lever = validate_lever(lever)
    owner = validate_owner(owner)
    return FIELD_SEP.join((
        jump_link(pane),
        f"waits: {waits}",
        f"lever: {lever}",
        f"owner: {owner}",
    ))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Render one parked-worker lever row; refuse a lever that names no lever.",
    )
    p.add_argument("--pane", required=True,
                   help="pane id, - for a headless worker, or ? when a tab worker's pane id is unresolved")
    p.add_argument("--waits", required=True, help="what the worker is parked on")
    p.add_argument("--lever", required=True, help="the one act that clears it")
    p.add_argument("--owner", required=True, help="manager | operator | machine: <watcher>")
    args = p.parse_args(argv)

    try:
        print(render(args.pane, args.waits, args.lever, args.owner))
    except LeverError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
