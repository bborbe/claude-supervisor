#!/usr/bin/env python3
"""Print the attention STORE's open-item count and its per-producer split.

This is the companion reading to `attention-board.py`, and the pair exists because
the two numbers are different questions with different answers:

  attention-board.py   how many cards the BOARD RENDERED (undimmed `<li>` rows)
  attention-store.py   how many items the STORE holds open (`GET /api/1.0/attention`)

They disagree by construction, not by race: measured 2026-10-01 back to back
against a live store, the API returned 4 open items while the board rendered 2
undimmed cards, and the two API-only ids stayed API-only across repeated fetches
minutes apart. So a round that prints only the board line has no reading of how
much the fleet is actually carrying — and `Board page: 0 undimmed cards` beside a
store of 52 reads as an empty board, which is the misreading this line removes.

The split is what makes a flood attributable. Measured 2026-11-16 19:41, the store
held 52 open cards: `sweep-gate` 24, `attention-unforwarded-closers` 10, everything
else 18. A total alone cannot tell "two thirds of the board is cron noise" from
"two thirds is real operator gates", so the per-producer split and the cron share
print on the same line.

    Store: 9 open — attention-unforwarded-closers 8, 5b29b81a 1 · cron 8/9 (89%)

Rows are sorted by count descending, then producer id, so consecutive rounds
compare line-for-line. `cron` counts items whose `producer_kind` is `cron` — the
store's own classification, so the share covers every cron producer rather than a
hardcoded pair of ids.

⚠️ **`store unavailable` is a branch, not an error message.** Refused, timed out,
non-200 and unparseable all take that same branch — one exit code and one literal
on stdout — mirroring `attention-board.py`'s `board unavailable`. The caller prints
that literal in place of the line; the reason goes to stderr for diagnostics only.

⚠️ **The limit is a bound, not a paging cursor.** `--limit` defaults to 500 and a
page that comes back exactly full is reported `(truncated at <limit>)` rather than
silently undercounted — an undercount that reads as a quiet board is the failure
this script exists to prevent.

    exit 0   the count line is on stdout
    exit 3   the store was unreachable; `store unavailable` is on stdout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")

#: Producer kind the store assigns to a cron tick. The share is computed from this
#: field rather than from a list of producer ids, so a new cron producer is counted
#: without editing this file.
CRON_KIND = "cron"

UNAVAILABLE = "store unavailable"

EXIT_OK = 0
EXIT_UNAVAILABLE = 3


def fetch_items(store: str, timeout: float, limit: int) -> list[dict]:
    """Return the store's open items, or raise. Every failure is the caller's one branch."""
    url = f"{store}/api/1.0/attention?state=open&limit={limit}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        body = json.loads(resp.read().decode("utf-8", "replace"))
    # The store serves a bare list; accept an `{"items": [...]}` envelope too. A dict
    # WITHOUT a list `items` is refused rather than read as empty: defaulting to `[]`
    # would print `Store: 0 open` on a shape change, the silent undercount this
    # script exists to prevent.
    if isinstance(body, dict):
        items = body.get("items")
        if not isinstance(items, list):
            raise ValueError("unexpected response shape: object without an items list")
        body = items
    if not isinstance(body, list):
        raise ValueError(f"unexpected response shape: {type(body).__name__}")
    return body


def producer_split(items: list[dict]) -> list[tuple[str, int]]:
    """Count items per `producer_id`, descending by count then id."""
    counts: dict[str, int] = {}
    for item in items:
        producer = item.get("producer_id") or "<unknown>"
        counts[producer] = counts.get(producer, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def cron_count(items: list[dict]) -> int:
    """Items the store classifies as a cron producer."""
    return sum(1 for item in items if item.get("producer_kind") == CRON_KIND)


def share_line(total: int, cron: int) -> str:
    """The cron share as `cron <c>/<n> (<pct>%)`, or `(n/a)` on an empty store."""
    if total == 0:
        return f"cron {cron}/{total} (n/a)"
    return f"cron {cron}/{total} ({round(100 * cron / total)}%)"


def render_line(items: list[dict], limit: int) -> str:
    total = len(items)
    parts = ", ".join(f"{producer} {n}" for producer, n in producer_split(items))
    line = f"Store: {total} open"
    if parts:
        line += f" — {parts}"
    line += f" · {share_line(total, cron_count(items))}"
    if total >= limit:
        line += f" (truncated at {limit})"
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the attention store's open-item count and per-producer split.",
    )
    parser.add_argument(
        "--store",
        default=STORE,
        help=f"store base URL (default {STORE}, or $ATTENTION_STORE_URL)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds to wait for the store (default 5)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="maximum items to read; a full page is reported truncated (default 500)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON object instead of the line",
    )
    args = parser.parse_args(argv)

    store = args.store.rstrip("/")
    try:
        items = fetch_items(store, args.timeout, args.limit)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # One branch for every failure, as `attention-board.py` does for the board.
        print(UNAVAILABLE)
        print(f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return EXIT_UNAVAILABLE

    if args.json:
        print(
            json.dumps(
                {
                    "open": len(items),
                    "producers": dict(producer_split(items)),
                    "cron": cron_count(items),
                    "truncated": len(items) >= args.limit,
                    "available": True,
                }
            )
        )
    else:
        print(render_line(items, args.limit))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
