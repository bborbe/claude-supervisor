#!/usr/bin/env python3
"""Print the attention store's open-card count and its per-producer split.

`attention-board.py` counts the board PAGE (undimmed rendered cards); this counts
the STORE (`GET /api/1.0/attention?state=open`). The two are deliberately
different numbers — the board page renders fewer cards than the store holds — so a
round that prints only the board line reports `0 open` while the store holds every
card. Measured 2026-11-16 20:47: the board line read 0, the store held 13.

The line is what makes the open-card volume observable. A cron producer that mints
one card per goal per tick grows the store while every individual producer behaves
as designed, so no producer-local check can bound it; only a round-level count can.

    Open: 13 — attention-unforwarded-closers 8, sweep-gate 5 (cron 100%)

Cron share is read from each item's `producer_kind` (`cron`), never from a
hardcoded producer list, so a new cron producer is counted without an edit here.

    exit 0   the count line is on stdout
    exit 3   the store was unreachable; `Open: store unavailable` is on stdout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")

# The store paginates; a round must see the whole open set or the split is a
# sample. 500 is the same bound the runbook's hand command uses.
LIMIT = 500

UNAVAILABLE = "Open: store unavailable"

EXIT_OK = 0
EXIT_UNAVAILABLE = 3


def fetch_open(store: str, timeout: float) -> list[dict]:
    """Return the store's open items, or raise. Every failure is the caller's one branch."""
    url = f"{store}/api/1.0/attention?state=open&limit={LIMIT}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        data = json.loads(resp.read().decode("utf-8", "replace"))
    # The live endpoint answers a bare list; accept a wrapped one so a future
    # envelope does not turn a readable store into an unreadable line.
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        raise ValueError(f"unexpected payload type: {type(data).__name__}")
    return data


def summarise(items: list[dict]) -> dict:
    """Count open cards by producer and compute the cron share."""
    by_producer = Counter(str(item.get("producer_id") or "?") for item in items)
    cron = sum(1 for item in items if item.get("producer_kind") == "cron")
    total = len(items)
    return {
        "open": total,
        "by_producer": dict(by_producer.most_common()),
        "cron": cron,
        "cron_share": (cron / total) if total else None,
    }


def render(summary: dict) -> str:
    """One line: total, per-producer split, cron share."""
    total = summary["open"]
    if not total:
        return "Open: 0"
    split = ", ".join(f"{pid} {n}" for pid, n in summary["by_producer"].items())
    share = summary["cron_share"]
    pct = f" (cron {round(share * 100)}%)" if share is not None else ""
    return f"Open: {total} — {split}{pct}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the attention store's open-card count and per-producer split.",
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
        "--json",
        action="store_true",
        help="emit one JSON object instead of the line",
    )
    args = parser.parse_args(argv)

    store = args.store.rstrip("/")
    try:
        items = fetch_open(store, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # One branch for every failure. The reason goes to stderr so stdout stays
        # the single literal the caller prints in place of the count line.
        print(UNAVAILABLE)
        print(f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return EXIT_UNAVAILABLE

    summary = summarise(items)
    if args.json:
        print(json.dumps({**summary, "url": store, "available": True}))
    else:
        print(render(summary))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
