#!/usr/bin/env python3
"""Every key a hand-back's consumer tests must actually be emitted by that hand-back.

Two defects, one class: a consumer that tests a key nobody emits. The key is then
permanently false, and the branch behind it silently never runs.

  (1) **The fleet tick's router** gates a step on a hand-back **count** — `the hand-back
      reports `blocked` > 0`. A key the sweep reader never emits makes that cell false
      forever, so the step silently never runs. That is the same defect class
      `check-step-when.py` catches for a drifted `When:` line, one layer down: there the
      trigger and its body disagree, here the trigger and the data it tests do.
  (2) **The delegated legs.** Since 2026-11-06 `commands/steps/round-readings.md` and
      `commands/steps/goal-spawn.md` each dispatch a reader agent
      (`agents/round-readings-reader.md`, `agents/goal-spawn-reader.md`), since 2026-11-07
      the fleet round's two acting legs do the same (`commands/steps/keepalive.md` →
      `agents/fleet-keepalive-actor.md`, `commands/steps/flagged-open.md` →
      `agents/fleet-flagged-open-actor.md`), and since 2026-11-08 the fleet round's open leg
      does the same (`commands/steps/open.md` → `agents/fleet-open-actor.md`) — each returns
      the verdicts the step no longer computes itself. A step reverted to its pre-split text
      stops dispatching, and a reader that drops one value of a family leaves the caller a
      cell it cannot evaluate — both silently. `docs/handback-contract.md` § The shape — the
      readings leg's required set — requires **every value of every family, including the
      zeros**, and the spawn leg's required set is one line per handed-over row. ⚠️ **Cited by bullet,
      never by line number:** this PR re-flowed that file, and a `:NN` citation is a second
      counter that silently points at whatever now sits on that line.

It is not hypothetical. The fleet hand-back was introduced with the class `held` present in
the digest render and absent from the key set, and `held` is the class the render treats as
winning over every other — so the one bucket a reader most needed was the one no cell could
see. The readings leg shipped the same shape: `in_flight()` and `external_wait()` both return
`unread`, but the hand-back carried one shared `unread=` (the feed's), so two of the three
values each family can take had no key at all.

**Scope, stated precisely.** For the fleet tick this asserts the keys a router `When` cell
*tests* — today `orphan`, `collision`, `unmanaged`, `blocked`, `context70`. For the delegated
legs it asserts each leg's **declared required set** (`LEGS` below) — the two manager-round
readers and the fleet round's three acting legs. It is still not a check of the whole
contract: buckets no cell tests and no leg declares are out of reach, so
`held`'s original absence would pass here on the fleet side. Widening it further means reading
a required set from somewhere this script does not own; until then it does not claim to.

Reads both artifacts directly: no registry, no build, no network.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROUTER = "commands/fleet-drive.md"
SWEEP = "agents/fleet-sweep-reader.md"

# `the hand-back reports `orphan`, `collision` or `unmanaged` > 0` — the count-test cell shape.
CELL = re.compile(r"hand-back reports\s+((?:`[a-z0-9_]+`(?:\s*,\s*|\s+or\s+)?)+?)\s*>\s*0")
KEY = re.compile(r"`([a-z0-9_]+)`")
#: A key as it appears in a hand-back fence. ⚠️ **Hyphens are in the class on purpose:** the
#: readings leg's `inflight-unread` / `external-unread` are single keys, and a class of
#: `[a-z0-9_]+` matches only their tail `unread` — which is exactly the collapse the widened
#: vocabulary exists to remove, and it would have passed this check while emitting nothing.
EMITTED_KEY = re.compile(r"\b([a-z][a-z0-9_-]*)=")

#: The legs the split added: the step that dispatches, the agent that answers, and the key set
#: the leg's contract requires. ⚠️ One entry per leg — a leg registered here is guarded in both
#: directions: its step must keep dispatching it, and its hand-back must keep emitting every
#: required key.
LEGS = {
    "supervisor:round-readings-reader": {
        "agent": "agents/round-readings-reader.md",
        "sites": ("commands/steps/round-readings.md",),
        "required": (
            "readings", "rows", "payload",
            # did the tracked set resolve to anything? `unresolved` / `empty` is the
            # refusal — a document that observed nothing must not reach the caller as a
            # quiet fleet, and the caller cannot tell without these two keys.
            "resolution", "tracked", "unresolved",
            # liveness
            "live", "absent", "unknown",
            # in-flight — all four values, the `unread` under its own key
            "inflight", "parked", "quiet", "inflight-unread",
            # attention feed
            "gated", "clear", "unread",
            # external wait — all three values, the `unread` under its own key
            "external", "none", "external-unread",
            "stuck",
        ),
    },
    "supervisor:goal-spawn-reader": {
        "agent": "agents/goal-spawn-reader.md",
        "sites": ("commands/steps/goal-spawn.md",),
        "required": (
            "rows", "open", "resume", "held", "refused", "truncated",
            "workers", "target", "cap", "payload",
            # the a2a branch's required argument — never emitted before the 2026-11-06 fix
            "task",
        ),
    },
    # The fleet round's two acting legs (rows 5b / 5c). Their counts reach the round's DIGEST,
    # so the hand-back is the only place the caller learns whether the write landed — a leg
    # whose `legs=` vanished would render counts no digest carries, and the render reads the
    # digest. `dead-scan` is emitted because the digest write branches on it: it is the
    # `--floor` / `--exact` label, and inferring it from the count is the "a number with no
    # label reads as a measurement" defect `output-shape.md` item 14 exists to remove.
    "supervisor:fleet-keepalive-actor": {
        "agent": "agents/fleet-keepalive-actor.md",
        "sites": ("commands/steps/keepalive.md",),
        "required": (
            "legs", "legs-reason", "lapsed-managers", "topicless-goal-managers", "dead-workers",
            "dead-scan",
            "restarted", "resumed", "parked-due", "nudged", "undelivered",
            # one count per truncatable row list: the caller's reconciliation is
            # `rows printed + that list's dropped count == this count`, so a list without
            # its count has no arithmetic to check and a row dropped in transit is invisible
            "crash-loop", "cannot-tell",
            # ⚠️ ONE DROPPED COUNT PER LIST, never one shared `truncated=`: two truncatable
            # lists behind a single count cannot say which list lost rows, so the per-list
            # reconciliation above is unsatisfiable. A one-list leg (flagged-open) keeps
            # `truncated`.
            "crash-loop-truncated", "cannot-tell-truncated",
            # the size-cap exception's payload key
            "payload",
        ),
    },
    "supervisor:fleet-flagged-open-actor": {
        "agent": "agents/fleet-flagged-open-actor.md",
        "sites": ("commands/steps/flagged-open.md",),
        "required": (
            "legs", "legs-reason", "operator-flagged", "opened", "refused", "truncated", "payload",
        ),
    },
    # The fleet round's open leg (row 16). Unlike the keep-alive and flagged-open legs it writes
    # NO digest key — `fleet-legs.py` carries three and none is this leg's; its persisted reading
    # is the `.open` block payload that `commands/steps/output-shape.md` item 3 renders. So the
    # hand-back is the only channel for every verdict the step acts on, and `render` / `reason`
    # are the two a dropped key silently breaks: a `status=ok` with no `render=` prints no block
    # at all, and a `status=not-run` with no `reason=` renders `open leg: not run — ` with nothing
    # after the dash. `refused` is the command's own tally, `truncated` is the difference between
    # it and the `refused:` lines that fit, and `selected` is what `opened` is reconciled against.
    # ⚠️ **`resumed` and `jumped` are required because the reconciliation is four-way, not two.**
    # `/supervisor:open` reports three branches — `↪️ jumped` / `♻️ resumed <sid>` / `🆕 created
    # <sid>` — and a resumed row is reachable for a selected row (`approved-not-started.py`: a row
    # whose session ENDED is not started). Without them, `opened + refused == selected` is false on
    # any round that activated a live session or resumed a dead one, so a leg that dropped either
    # key would silently leave the caller unable to reconcile at all.
    "supervisor:fleet-open-actor": {
        "agent": "agents/fleet-open-actor.md",
        "sites": ("commands/steps/open.md",),
        "required": (
            "status", "selected", "opened", "resumed", "jumped", "refused", "render",
            "reason", "truncated", "payload",
        ),
    },
}


def keys_tested(text: str) -> set[str]:
    found: set[str] = set()
    for m in CELL.finditer(text):
        found.update(KEY.findall(m.group(1)))
    return found


def keys_emitted(text: str) -> set[str]:
    """Keys in the fenced block inside `<output_format>`'s hand-back section.

    Scoped to the fence on purpose. Scanning the whole section accepts any `word=` in the
    surrounding prose — `key=value` alone yields a spurious `key`, and a synthetic case
    with `blocked=` only in prose would satisfy the guard while the block lacked it. A
    guard that passes on prose is the false-green this file exists to avoid.
    """
    _, sep, rest = text.partition("**Hand-back**")
    if not sep:
        return set()
    body = rest.partition("</output_format>")[0]
    fenced = re.search(r"```[a-z]*\n(.*?)```", body, re.DOTALL)
    if not fenced:
        return set()
    return set(EMITTED_KEY.findall(fenced.group(1)))


def check_fleet(root: Path) -> list[str]:
    """The fleet tick's router cells against the sweep reader's hand-back."""
    router = root / ROUTER
    sweep = root / SWEEP
    if not router.exists():
        return [f"{ROUTER}: missing — the fleet router's hand-back cells cannot be read"]
    if not sweep.exists():
        return [f"{SWEEP}: missing — the fleet sweep hand-back cannot be read"]
    tested = keys_tested(router.read_text(encoding="utf-8"))
    if not tested:
        return [
            "no count-test When cell found in the fleet router — either the cell shape "
            "changed or the router lost its hand-back cells"
        ]
    emitted = keys_emitted(sweep.read_text(encoding="utf-8"))
    return [
        f"`{k}` is tested by a fleet router When cell but never emitted by the sweep "
        f"hand-back, so its cell is false forever"
        for k in sorted(tested - emitted)
    ]


def check_legs(root: Path) -> list[str]:
    """Each delegated leg: its step still dispatches it, its hand-back emits its key set."""
    failures: list[str] = []
    for name, leg in sorted(LEGS.items()):
        agent_rel = leg["agent"]
        agent = root / agent_rel
        for site_rel in leg["sites"]:
            site = root / site_rel
            if not site.exists():
                failures.append(f"{site_rel}: missing — it is the dispatch site for {name}")
            elif name not in site.read_text(encoding="utf-8"):
                failures.append(
                    f"{site_rel}: no longer dispatches {name} — the step reads as its "
                    f"pre-split text, and the leg it was split into is never called"
                )
        if not agent.exists():
            failures.append(f"{agent_rel}: missing — {name}'s hand-back cannot be read")
            continue
        emitted = keys_emitted(agent.read_text(encoding="utf-8"))
        if not emitted:
            failures.append(f"{agent_rel}: no `**Hand-back**` fenced block — nothing to check")
            continue
        for k in sorted(set(leg["required"]) - emitted):
            failures.append(
                f"{agent_rel}: `{k}=` is required by the leg's contract but never emitted "
                f"in its hand-back, so the caller has a cell it cannot evaluate"
            )
    return failures


def main(root: Path | None = None) -> int:
    root = Path(root) if root is not None else REPO
    failures = check_fleet(root) + check_legs(root)
    if failures:
        print(f"handback-keys FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    tested = keys_tested((root / ROUTER).read_text(encoding="utf-8"))
    leg_keys = sum(len(leg["required"]) for leg in LEGS.values())
    print(
        f"handback-keys ok: {len(tested)} tested fleet key(s), all emitted by the sweep "
        f"hand-back; {len(LEGS)} delegated leg(s) dispatch and emit all {leg_keys} required "
        f"key(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
