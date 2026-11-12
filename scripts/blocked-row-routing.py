#!/usr/bin/env python3
"""blocked-row-routing — the routing record for a 🔴 BLOCKED row that names no owner.

`agents/fleet-drive.md` renders an `ESCALATION` row for every blocked session, and that
row carries the manager that owns the subject (`scripts/escalation-manager.py`). But the
decision of WHETHER to route at all was still prose: a closer that already names an owner
— a card id, a manager session, an `⏰ Ends:` observable — needs no routing, and one that
says only "waiting on someone" does. Measured 2026-11-10: 4 of 8 blocked sessions named no
recipient, so 4 rows were escalated to nobody while 4 others would have been double-routed.

⚠️ **This composes two shipped scripts; it re-derives neither.**
  * `closer-gate-lint.py`'s `owner_named()` owns "does this closer name an owner", including
    the two-haystack split (`👤 You:` body vs a separate `⏰ Ends:` line). A second copy here
    would drift from the lint that refuses an ownerless closer in the first place.
  * `escalation-manager.py`'s `resolve()` owns "which manager owns this subject", including
    the four-tier chain. `docs/fleet-surface.md` is its single statement and a second
    implementation is the drift that document forbids.

⚠️ **`waiting on someone` is NOT an owner.** It names a wait, not a recipient — the same
reading the lint takes when it refuses a `🔴 BLOCKED` line over one. This script therefore
routes it, rather than treating the phrase as a recipient.

⚠️ **`--closer` takes the BODY, never the whole `👤 You:` line.** `OWNER_RE`'s operator-verb
and `later (on` arms are line-anchored, so a prefixed body defeats exactly the closers they
exist for and every one of them routes. `closer-gate-lint.py`'s `closer_body()` is the producer
a wiring must use — nothing calls this script yet.

⚠️ **KNOWN FALSE NEGATIVE — a hex run in the closer reads as an owner.** `OWNER_RE`'s first
arm is `\b[0-9a-f]{8,32}\b`, so `waiting on commit 64b708ab`, `waiting on 20261112 nightly`
and `waiting on abc12345` all come back **NO-ROUTE** even though each names a wait, not a
recipient. That is a dropped escalation, i.e. the failure this script exists to end, and it
is silent. It is NOT fixed here: the predicate belongs to `closer-gate-lint.py`, and a
private copy is the drift this module forbids. `test_a_hex_wait_is_a_KNOWN_false_no_route`
pins the behaviour as documented-wrong, so a follow-up that wires this cannot read NO-ROUTE
as "no recipient" without meeting the test.

⚠️ **`resolve()` is unbounded.** A real call measured 270 s here (it reads the fleet board).
A hang is neither exit 0 nor exit 2 — the one silence the contract below does not cover.

Usage:
    blocked-row-routing.py --closer "waiting on someone" --task "<task title>"
    blocked-row-routing.py --closer "..." --row "<whole closer message>" --task "<title>"

Prints `ROUTE\\t<manager>\\t<source>` or `NO-ROUTE\\t<why>`. Exit 0 for either; 2 when the
manager cannot be resolved (an unreadable board is not "no routing").
"""
import argparse
import functools
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


@functools.lru_cache(maxsize=None)
def _load(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def routing(closer_body, row_text, task_title, resolver=None):
    """`{"manager": …, "source": …}` when the row must be routed, else None.

    `resolver` is injected so the tests exercise every branch without a board.
    """
    lint = _load("closer-gate-lint.py", "closer_gate_lint")
    if lint.owner_named(closer_body, row_text):
        return None
    if resolver is None:
        resolver = _load("escalation-manager.py", "escalation_manager").resolve
    manager, source = resolver(task_title)
    return {"manager": manager, "source": source}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--closer", required=True, help="the closer's 👤 You: body")
    parser.add_argument("--row", default=None, help="the whole closer message (for ⏰ Ends:)")
    parser.add_argument("--task", required=True, help="the subject's task title")
    args = parser.parse_args(argv)

    row = args.row if args.row is not None else args.closer
    try:
        record = routing(args.closer, row, args.task)
    except Exception as exc:
        # An unreadable board is NOT "no routing": silence would look like an owner was
        # found, which is the failure this script exists to end.
        print(f"blocked-row-routing: {exc} — routing UNKNOWN, not skipped", file=sys.stderr)
        return 2

    if record is None:
        print("NO-ROUTE\tthis closer already names an owner")
        return 0
    print(f"ROUTE\t{record['manager']}\t{record['source']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
