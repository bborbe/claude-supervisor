#!/usr/bin/env python3
"""Emit the reap hand-over — and only when the recipient can actually be reached.

A reap is decided from the **caller's roster**, and that roster is read from the
supervisor registry (`mcp__supervisor__list_agents`), which **retains ended
sessions**. The harness surface `SendMessage` addresses carries live sessions
only. So a reap whose remedy is *"nudge the session to close itself"* has no
recipient whenever the session has already ended — which is the common case for
a task reading `completed` with zero open boxes.

Measured 2026-10-03, Manager Layer topic manager tick 86 (session `4cc474d6`):
the drive leg returned `reaped 2` and handed both over as drafts. `agent_1`
(`99058101…`, ended 2026-09-30) and `agent_10` (`4cd2c43c…`) were both in the
registry and both absent from the harness's 43-row peer list; `SendMessage`
refused every spelling of either name, from the leg and from the caller. The
summary line read `reaped 2` — indistinguishable at a glance from two sessions
nudged. ⚠️ **The failure is silent in the same direction as the rest of its
family:** a payload that is self-consistent and plausible, differing from
reality with nothing naming the difference.

The fix is the shape `scripts/close-line.py --liveness` already uses for the
close half: **a mechanical producer, not a dispatch-time paragraph.** Measured
2026-10-08 12:26 / 12:46 / 13:10 on one caller, one row, one liveness input: the
leg withheld, then attempted, then withheld again. The outcome tracked only an
explicit instruction in the dispatch, and a contract the caller must remember to
write is not a contract.

Usage:
  reap-line.py --task "<task name>" --session "<full session id>" --liveness <verdict>
               [--now <ISO-8601>] [--plugin-root <dir>]

`--liveness` is the caller's own verdict, taken this run — the caller owns it and
this script never re-derives it. `scripts/session-liveness.py` is the instrument,
and its five verdicts map onto this script's four inputs:

  LIVE (0)       → live     → the reap line, exit 0
  ABSENT (1)     → absent   → reap moot, exit 0
  UNKNOWN (2)    → unknown  → reap moot, exit 0
  AMBIGUOUS (3)  → unknown  → reap moot, exit 0
  MALFORMED (4)  → unknown  → reap moot, exit 0
  (not a probe verdict) → parked → reap moot, exit 0

⚠️ **AMBIGUOUS and MALFORMED collapse to `unknown`, never to `live` and never to
`absent`.** Each is a verdict about the **argument**, not the session: a prefix
matching two live sessions, or a string that is not an id at all, proves nothing
about liveness — and it certainly does not name a recipient.

⚠️ **`parked` is deliberately NOT deliverable here, unlike `close-line.py` where
it is runnable.** The close half is owed to a session that is live-but-parked;
the reap half is moot for one already accounted for by an open gate. The caller
supplies `parked` only when **the sweep classified the session parked** — it is
never a `session-liveness.py` verdict.

⚠️ **The exit code does not split delivered from moot.** A moot reap is a normal
outcome, not an error — the row is terminal and its recipient is gone — so both
renders exit 0 and the **line itself** is the gate: the only render carrying
`SendMessage` is the one that may be sent. Exit 2 is a usage error. This is
deliberately unlike `close-line.py`, whose 0/1 split exists so a CLI consumer
can branch; here the caller is an agent reading stdout, and a `check=True`
caller must not raise on the common case. A caller that wants to branch reads
the line.

Output is one line on stdout. Exit 0 for either render, 2 for a usage error.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

LIVENESS_LIVE = "live"
LIVENESS_PARKED = "parked"
LIVENESS_ABSENT = "absent"
LIVENESS_UNKNOWN = "unknown"

LIVENESS_WORDS = (LIVENESS_LIVE, LIVENESS_PARKED, LIVENESS_ABSENT, LIVENESS_UNKNOWN)

# The one verdict that names a reachable recipient. Every other word — including
# `parked`, which is live-but-gated — renders moot; see the module docstring.
DELIVERABLE = (LIVENESS_LIVE,)

# The whole of the moot render, verbatim and with no task name. SC1 requires this
# exact line, and the caller prefixes the task in its own report — the script's
# job is the verdict, not the frame.
MOOT = "reap moot — no live recipient"

# The evidence the reap message carries. It is NON-AUTHORISING by design: the leg
# sends the disk facts and says the operator has not answered. Never soften this
# into an instruction to close — `agents/manager-drive.md` states the rule.
REAP_EVIDENCE = (
    "status: completed · phase: done · 0 open boxes — non-authorising, "
    "the operator has not answered; close yourself with "
    "/vault-cli:sync-progress + /vault-cli:session-close"
)

# The plugin root, resolved the way `close-line.py` resolves its re-check command:
# an absolute expanduser path rather than an inherited `${CLAUDE_PLUGIN_ROOT}`,
# so the line is runnable by a reader whose shell does not carry the variable.
# `--plugin-root` and `REAP_LINE_PLUGIN_ROOT` both override, so a fixture can
# exercise the line without reading the operator's real install.
DEFAULT_PLUGIN_ROOT = "~/.claude/plugins/marketplaces/claude-supervisor"


def plugin_root(override=None):
    """Resolve the plugin root the re-check command is built from."""
    return os.path.expanduser(
        override or os.environ.get("REAP_LINE_PLUGIN_ROOT") or DEFAULT_PLUGIN_ROOT
    )


def recheck_command(session, root=None):
    """The exact command a reader runs to re-settle the recipient's liveness.

    The full session id, never a prefix: `session-liveness.py` accepts an 8-char
    prefix for its own convenience, but the id embedded here is the one the
    caller holds, and shortening it would hand the reader a weaker probe than the
    caller had. The prefix form is also the measured trap — it returns AMBIGUOUS
    or MALFORMED, the branches that say "not a recipient".
    """
    return f"python3 {plugin_root(root)}/scripts/session-liveness.py --check {session}"


def stamp(now=None):
    """The emission moment, ISO-8601 in UTC, second precision, `Z`-suffixed."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sid8(session):
    """The display handle. Short here is correct — the full id rides the line."""
    return session[:8]


def reap_line(task, session, liveness, now=None, root=None):
    """The deliverable reap line, or the moot verdict that carries no recipient.

    Only `live` delivers. See the module docstring for the vocabulary and why
    `parked` is not deliverable here even though `close-line.py` treats it as
    runnable.
    """
    if liveness not in DELIVERABLE:
        return MOOT
    return (
        f"✅ REAP: {task} [{sid8(session)}] — verified {stamp(now)} "
        f"· SendMessage {session}: {REAP_EVIDENCE} "
        f"· re-check: {recheck_command(session, root)}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Emit the reap hand-over, or the moot verdict when the "
                    "recipient is not live."
    )
    parser.add_argument("--task", required=True, help="the task name, verbatim")
    parser.add_argument("--session", required=True, help="the FULL session id")
    parser.add_argument(
        "--liveness",
        required=True,
        choices=LIVENESS_WORDS,
        help="the caller's own verdict, taken this run (map the probe's "
             "AMBIGUOUS/MALFORMED to `unknown`)",
    )
    parser.add_argument("--now", default=None, help="override the stamp (ISO-8601)")
    parser.add_argument("--plugin-root", default=None, help="override the plugin root")
    args = parser.parse_args(argv)

    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(f"reap-line: --now is not ISO-8601: {args.now}", file=sys.stderr)
            return 2

    print(reap_line(args.task, args.session, args.liveness, now, args.plugin_root))
    # Both renders are normal outcomes; only a usage error is non-zero. The line
    # is the gate — see the module docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main())
