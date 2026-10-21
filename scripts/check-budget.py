#!/usr/bin/env python3
"""The context-budget ratchet: command, agent and skill bodies may not grow back.

The harness re-injects a command's or skill's full body on every invocation, so an
oversized body is paid again on every tick. Eight files in this repo are already over
the 30 KB body budget and this change may not shrink the seven it does not own, so the
gate is a **ratchet** rather than a zero-output check: it holds every file at or below
its recorded size and refuses both directions of drift.

**Tighten-or-fail.** It fails when a baselined file is ABOVE its recorded max (a
regression) and also when one is BELOW it (a stale baseline). The second direction is
the point: without it, a shrink is silently given back on the next edit and the file
regrows one incident at a time, which is the defect this exists to prevent. The remedy
is `--tighten`, which lowers only the entries that are genuinely under their recorded
max.

**`--tighten` never rewrites the whole file.** Every entry at or above its recorded max
is left byte-identical. That is deliberate and it is what makes a rebase safe: a second
branch running `--tighten` before this one lands cannot lower this one's entries, because
those files are at their max rather than under it.

An entry whose file has disappeared is a failure too — the baseline has drifted from the
tree — and `--tighten` drops it, since a deleted file has no size to hold.

⚠️ **The strict cap follows the WAKEUP TARGET, never a filename pattern alone.** A wakeup
re-injects whatever prompt it names, so that body — and only that body — is paid again on
every tick. Until 2026-10-11 the target was always a `commands/*-tick.md`, so a glob found
them all. The manager loop's wakeup names a tier round (`/supervisor:goal-drive` or
`/supervisor:topic-drive`), a round with no `-tick` suffix, and a glob-only rule would
have applied the cap to nothing on that side while reporting green. Both spellings are therefore checked: the glob (the fleet loop still
names `commands/goal-drive.md`) plus an explicit list of the targets that carry no suffix.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "budget-baseline.json"
BODY_MAX = 30 * 1024
TICK_MAX = 10 * 1024
SCAN_DIRS = ("commands", "agents", "skills")

#: Wakeup targets that carry no `-tick` suffix, so the glob below cannot find them.
#: `commands/goal-drive.md` is the manager loop's round since 2026-10-11 — the prompt
#: `/supervisor:goal-loop` arms `ScheduleWakeup` with. `commands/fleet-drive.md` is the
#: fleet loop's round since 2026-10-12, when the former fleet round command was folded
#: into it and the `*-tick.md` glob stopped matching anything on the fleet side. A target
#: missing from this list is a body the strict cap silently stops covering.
#: ⚠️ One entry per WAKEUP NAMED, not per tier: since the 2026-10-20 topic/goal split there
#: are two manager rounds, and a target missing here is a body the strict cap silently stops
#: covering while the check still reports green.
WAKEUP_TARGETS = (
    "commands/topic-drive.md",
    "commands/goal-drive.md",
    "commands/fleet-drive.md",
)


def load() -> dict:
    if not BASELINE.exists():
        print(f"budget FAILED: {BASELINE.relative_to(REPO)} does not exist", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def size_of(rel: str) -> int | None:
    p = REPO / rel
    return p.stat().st_size if p.exists() else None


def main(argv: list[str]) -> int:
    tighten = "--tighten" in argv
    data = load()
    files: dict[str, int] = data["files"]

    failures: list[str] = []
    lowered: dict[str, int] = {}
    dropped: list[str] = []

    for rel, recorded in sorted(files.items()):
        current = size_of(rel)
        if current is None:
            if tighten:
                dropped.append(rel)
                continue
            failures.append(f"{rel}: baselined at {recorded} but the file is gone (run --tighten)")
            continue
        if current > recorded:
            failures.append(
                f"{rel}: {current} exceeds its recorded max {recorded} — it grew. "
                f"Shrink it, or if the growth is deliberate raise the entry by hand "
                f"(--tighten cannot, by design)."
            )
        elif current < recorded:
            if tighten:
                lowered[rel] = current
            else:
                failures.append(
                    f"{rel}: {current} is UNDER its recorded max {recorded} — the baseline "
                    f"is stale and would let the file regrow to {recorded}. Run "
                    f"`scripts/check-budget.py --tighten` and commit the result."
                )

    # A file that is over budget and absent from the baseline is a new oversized body.
    for d in SCAN_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            rel = str(p.relative_to(REPO))
            if rel in files:
                continue
            n = p.stat().st_size
            if n > BODY_MAX:
                failures.append(
                    f"{rel}: {n} is over the {BODY_MAX} body budget and is not baselined — "
                    f"new oversized bodies are refused outright, not ratcheted."
                )

    # The wakeup target is the one class with a strict cap: it is what a wakeup injects.
    tick_dir = REPO / "commands"
    if tick_dir.is_dir():
        for p in sorted(tick_dir.glob("*-tick.md")):
            n = p.stat().st_size
            if n > TICK_MAX:
                failures.append(
                    f"{p.relative_to(REPO)}: {n} exceeds the {TICK_MAX} tick budget. "
                    f"A tick is re-injected on every wakeup, so this cap is strict."
                )

    # The wakeup targets that carry no `-tick` suffix, so the glob above cannot reach them.
    # A target named here but gone is a failure rather than a skip: a cap pointed at a file
    # that no longer exists reports green against a tree it never measured.
    for rel in WAKEUP_TARGETS:
        p = REPO / rel
        if not p.exists():
            failures.append(
                f"{rel}: named as a wakeup target but the file is gone — the strict tick "
                f"cap would otherwise report green against nothing"
            )
            continue
        n = p.stat().st_size
        if n > TICK_MAX:
            failures.append(
                f"{rel}: {n} exceeds the {TICK_MAX} tick budget. It is the manager loop's "
                f"wakeup target, re-injected on every wakeup, so this cap is strict."
            )

    if lowered or dropped:
        for rel, n in lowered.items():
            files[rel] = n
        for rel in dropped:
            files.pop(rel, None)
        # ensure_ascii=True: reproduce the baseline file's own escaping so a tighten moves
        # only the entries that changed. Re-encoding non-ASCII rewrites every escaped
        # character in the `_note`, burying the one changed number under a whole-file diff.
        BASELINE.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        for rel, n in sorted(lowered.items()):
            print(f"budget tightened: {rel} -> {n}")
        for rel in sorted(dropped):
            print(f"budget dropped: {rel} (file gone)")

    if failures:
        print(f"budget FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"budget ok: {len(files)} baselined file(s) at or under their recorded max; no new oversized body")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
