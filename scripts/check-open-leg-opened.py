#!/usr/bin/env python3
"""A round that refused EVERY selected row must fail loudly, not render as `opened 0`.

The open leg's block reads the same whether it refused all rows or found nothing to open, and the
round's tally prints `opened: 0 of N` in the same shape as a round that legitimately resumed rows.
Measured 2026-11-18: round `1795012825` refused all 7 selected rows with `SESSION UNREADABLE` and
the round rendered `opened 0` — a silent fleet-wide stop-start.

⚠️ **The counts come from the hand-back, never from the `.open` payload.** The payload is the
block `open-queue.py` printed; the tally and the `refused:` lines are printed by the round from the
**hand-back**, and only truncation overflow reaches `.open-refusals`. Measured 2026-11-18 across
`~/.claude/state/fleet-handback/`: **1 of 90** `.open` files carries an `opened: …` line at all —
a check that parsed the payload would answer "not a verdict" on every real round, which is why this
one is handed the numbers the round already has.

⚠️ **`opened` alone is not the test.** A row that is `♻️ resumed` or `↪️ jumped` re-enters a live
session and is a success, so a round with `opened 0 · resumed 3 · refused 1` (round `1795020429`)
is healthy and must pass. The failure condition is *no selected row reached a live session at all*:
`opened + resumed + jumped == 0` while `selected > 0`.

Usage:
    check-open-leg-opened.py --selected N --opened N --resumed N --jumped N [--refused N]

Exit: 0 rows reached live sessions (or nothing was selected) · 1 every selected row was refused ·
2 not a verdict (a count missing or unparseable — never a pass).
"""

import argparse
import sys


def fail(message: str) -> None:
    # ⚠️ Exit **2**, never 1: 1 is this check's "every selected row was refused" verdict, and a
    # missing or unparseable count must not borrow it — a round whose numbers could not be read
    # has not established that anything was refused.
    print("❌ " + message, file=sys.stderr)
    sys.exit(2)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    for name in ("--selected", "--opened", "--resumed", "--jumped", "--refused"):
        parser.add_argument(name, required=name != "--refused", type=int)
    try:
        return parser.parse_args(argv)
    except SystemExit:
        fail("usage: check-open-leg-opened.py --selected N --opened N --resumed N --jumped N "
             "[--refused N]")


def verdict(selected: int, opened: int, resumed: int, jumped: int, refused: int) -> tuple[int, str]:
    if selected < 0 or opened < 0 or resumed < 0 or jumped < 0 or refused < 0:
        return 2, "negative count — not a verdict"
    if selected == 0:
        return 0, "open leg: selected 0 — nothing to open"
    reached = opened + resumed + jumped
    if reached > 0:
        return 0, ("open leg: %d of %d reached a live session (opened=%d resumed=%d jumped=%d "
                   "refused=%d)" % (reached, selected, opened, resumed, jumped, refused))
    return 1, ("❌ open leg: EVERY selected row was refused — selected %d, opened 0, resumed 0, "
               "jumped 0, refused %d" % (selected, refused))


def main() -> None:
    args = parse_args(sys.argv[1:])
    code, line = verdict(args.selected, args.opened, args.resumed, args.jumped, args.refused or 0)
    print(line, file=sys.stderr if code else sys.stdout)
    sys.exit(code)


if __name__ == "__main__":
    main()
