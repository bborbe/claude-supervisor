#!/usr/bin/env python3
"""Print the attention board's own open-card count, with the board URL.

A manager that used to hand the operator one jump link per open gate prints ONE
line naming the board and how much is waiting on it, and lets the operator work
from the board instead of from a chat list that dies with the turn that wrote it.

**The count is read from the BOARD, not from the store API.** `GET
/api/1.0/attention` is not the board's row set: measured 2026-10-01, back to back
against a live store, the API returned 4 open items while the board rendered 2
undimmed cards, and the same two ids stayed API-only across repeated fetches
minutes apart — so it is a stable difference, not a race between the two reads.
The board is authoritative for its own card count by construction: counting the
cards it actually rendered cannot drift from the page the operator is looking at,
which is the one failure this line exists to prevent.

The board marks an OPEN card `<li class="item" data-item-id="...">` and an
ANSWERED one `<li class="item dimmed" data-item-id="...">`
(`pkg/handler/attention-page.go`, attention-controller). Counting the exact string
`class="item" data-item-id=` therefore counts open cards and nothing else:
`class="item dimmed"` does not contain it. The board's own "Hide answered" switch
is a client-side toggle over markup that already carries both kinds, so it does
not change what this counts.

⚠️ **The page carries the whole history**, so it is ~2.6 MB against a live store
and grows with it. That is the accepted cost of reading the board rather than the
API: the two disagree, and the board is the one the operator acts on.

⚠️ **`board unavailable` is a branch, not an error message.** A manager whose
store cannot be reached falls back to chat jump links and says so in one line.
Refused, timed out, non-200 and unparseable all take that same branch, so they
share one exit code and one line — the caller has nothing different to do about
any of them.

    exit 0   the count line is on stdout
    exit 3   the board was unreachable; `board unavailable` is on stdout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")

# The board's open-card marker, exact. `<li class="item dimmed" ...>` does not
# contain it, which is what makes this a count of open cards rather than of cards.
OPEN_CARD = '<li class="item" data-item-id="'

UNAVAILABLE = "board unavailable"

EXIT_OK = 0
EXIT_UNAVAILABLE = 3


def fetch_board(store: str, timeout: float) -> str:
    """Return the board page, or raise. Every failure is the caller's one branch."""
    with urllib.request.urlopen(store + "/", timeout=timeout) as resp:
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        return resp.read().decode("utf-8", "replace")


def count_open_cards(html: str) -> int:
    """Count the board's undimmed cards. See the module docstring for the marker."""
    return html.count(OPEN_CARD)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the attention board's open-card count and URL.",
    )
    parser.add_argument(
        "--store",
        default=STORE,
        help=f"board base URL (default {STORE}, or $ATTENTION_STORE_URL)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds to wait for the board (default 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON object instead of the line",
    )
    args = parser.parse_args(argv)

    store = args.store.rstrip("/")
    try:
        html = fetch_board(store, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # One branch for every failure. The reason goes to stderr so the caller's
        # chat line stays the single literal the fallback contract names.
        print(UNAVAILABLE)
        print(f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return EXIT_UNAVAILABLE

    open_cards = count_open_cards(html)
    if args.json:
        print(json.dumps({"open": open_cards, "url": store, "available": True}))
    else:
        print(f"Board: {open_cards} open — {store}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
