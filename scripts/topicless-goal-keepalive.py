#!/usr/bin/env python3
"""Retired: the fleet tier's keep-alive for goal managers whose goal no topic declares.

The goal-manager tier is merged into the topic tier (2026-11-24). There is no
goal-manager session to keep alive, so this leg has nothing to select and nothing to
resume: a topicless goal is driven by the fleet round directly, through
`agents/goal-drive.md`, not by a keeper that restarts a goal manager.

This file is kept as a refusing stub rather than deleted, so a caller still invoking it
(the fleet round's `commands/steps/keepalive-topicless-goals.md`, an older install, a
hand-run) stops loudly with a reason instead of failing as "command not found" — which
reads like a broken install rather than a retired tier.

Exit codes: 1 retired (every invocation) · 2 argparse refused its own arguments.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, help="the vault ROOT path (ignored)")
    ap.add_argument("--goals-dir", help="ignored; accepted for caller compatibility")
    ap.add_argument("--topics-dir", help="ignored; accepted for caller compatibility")
    ap.parse_args(argv)
    print("retired")
    print("the goal-manager tier is merged into the topic tier; a topicless goal is driven "
          "by the fleet round directly through agents/goal-drive.md — this keeper is retired.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
