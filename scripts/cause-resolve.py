#!/usr/bin/env python3
r"""Resolve ONE session's cause-of-end from that session's own sources, or say unverified.

Measured 2026-11-14, round 60. A fleet round "corrected" session `8c2933b4`'s cause from
`drain` to `reaper`, citing `verb=reap … trigger=idle-15min`. That line belonged to a
**different** session: the reap tick in `9b08cde6`'s events file names `80288603`,
`61c6c69b` and `f8052f51`, none of them `8c2933b4`. The round read a sibling's log and
reported it as this session's cause, in a confident register. The Fleet Manager then
produced a *second* wrong cause for the same session — `drain` — from a line that named
the session but described a turn boundary rather than an end.

Both failures share one mechanism: the cause was assembled from whatever text was nearest,
not from a line that both **names the session** and **states the action**.

This resolver is the deterministic replacement for that search. It reads exactly two
things — the target session's own `events.jsonl` and the state ledger — and never opens a
sibling's directory. A cause it cannot find there is reported as `unverified`, which is a
first-class verdict rather than a widened permission: the caller renders it, and a round
that has no evidence says so instead of borrowing one.

## What counts as an assertion

A line asserts a cause only when it names the session **and** states what happened to it.
The vocabulary is the wrapper's own, not invented here (`bborbe/claude-wrapper`):

  - `idle-reaper-tick session=<sid> verb=reap outcome=<o> trigger=<t>` — the Python tick
    (`idle-reaper-tick.py`, retired 2026-11 into the daemon). A refusal outcome
    (`not-idle-long-enough`, `busy-target`, `changed-since-list`, `stop-failed`) is the
    reaper saying it did **not** reap, so it is not a cause.
  - `job=idle-reaper action=closed sid=<sid> idle=<age>` — the current Go tick
    (`pkg/idle_reaper.go`). This one only exists when a session was actually closed.

A line that names the session without asserting an end is **not** a cause, and the shape
that produced the second false reading is the canonical example:

  - `drain: session <sid> at turn boundary (state <s>)` (`claude-wrapper/main.go`) — the
    drain *observes* a boundary. `pkg/supervisor.go` says so in as many words: "Drain
    waits for every child to reach a turn boundary … It never kills a child."

A bare `verb=reap` substring is not evidence either: it appears verbatim inside tool
results that merely *read* a log, including the task file that documents this bug.

## What it does not claim

The ledger (`~/.claude/state/state-ledger/events-YYYY-MM-DD.jsonl`) records that a session
closed and why it was judged absent (`reason: absent-from-sources`). It does **not** record
the mechanism, so the resolver reports the ledger row as context and never derives a cause
from it. Reading a fact from the store that owns it means not reading a mechanism out of a
store that never wrote one.

## Usage

    python3 scripts/cause-resolve.py --session <sid> [--json]
    python3 scripts/cause-resolve.py --session <sid> \
        --sessions-root <dir> --ledger <file> [--json]

`--sessions-root` and `--ledger` exist so a fixture can pin both inputs and the test is
deterministic — the production defaults read live state, which no test should depend on.
"""

import argparse
import datetime
import json
import pathlib
import re
import sys

#: One directory per session; the only file this resolver reads inside it.
SESSION_EVENTS_NAME = "events.jsonl"

#: The reaper's own vocabulary. A refusal means "did not reap", so it cannot be a cause.
REAP_REFUSAL_OUTCOMES = frozenset(
    {
        "not-idle-long-enough",
        "busy-target",
        "changed-since-list",
        "stop-failed",
        "refused",
        "skipped",
    }
)

_SID = r"(?P<sid>[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)"

#: The retired Python tick. `verb=reap outcome=<o>` is the assertion; `session=<sid>` ties
#: it to a target. Both must sit on one line — a log tail is one line per tick entry.
REAP_TICK = re.compile(
    r"idle-reaper-tick\s+session=" + _SID + r"\s+verb=reap\s+outcome=(?P<outcome>\S+)"
)

#: The current Go tick, one line per session it actually closed.
REAP_CLOSED = re.compile(r"job=idle-reaper\s+action=closed\s+sid=" + _SID)

#: Names the session, states an observation. Deliberately NOT a cause.
DRAIN_BOUNDARY = re.compile(r"drain:\s+session\s+" + _SID + r"\s+at turn boundary")


def _default_sessions_root():
    return pathlib.Path.home() / ".claude" / "state" / "claude-wrapper" / "sessions"


def _default_ledger():
    """Today's ledger file, derived from the clock at call time.

    Not a constant: the ledger is written per day, and a resolver pinned to one date
    would answer `no record` for every session that ended after midnight.
    """
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return pathlib.Path.home() / ".claude" / "state" / "state-ledger" / ("events-%s.jsonl" % day)


def _sid_matches(candidate, target):
    """A log line may carry the 8-char short form while the caller holds the full id."""
    if not candidate or not target:
        return False
    return candidate == target or target.startswith(candidate) or candidate.startswith(target)


def _iter_log_lines(path):
    """Yield every string that could be a log line in one session's events file.

    The file is a JSONL of Claude events, so the reaper's text arrives inside a
    `tool_result` (a session tailed the log) or an assistant `text` block. Each candidate
    is yielded whole rather than split on whitespace: the patterns above match a tick
    entry, and splitting would break `session=<sid> verb=reap` across candidates.
    """
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                # A non-JSON line is still text worth scanning: a truncated or
                # hand-written log must not read as "no cause found" by default.
                yield raw
                continue
            for text in _event_texts(event):
                yield text


def _event_texts(event):
    """Every text payload in one event, in file order."""
    message = event.get("message")
    if isinstance(message, str):
        yield message
        return
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        for key in ("text", "content"):
            value = block.get(key)
            if isinstance(value, str):
                yield value


def find_assertion(events_path, sid):
    """Return (cause, line) for the first line that names `sid` AND states an action.

    Scans only `events_path`. A sibling's file is never opened — the caller passes this
    session's own directory, and the sibling-borrow regression is what the test pins.
    """
    for line in _iter_log_lines(events_path):
        for match in REAP_CLOSED.finditer(line):
            if _sid_matches(match.group("sid"), sid):
                return "reaper", line
        for match in REAP_TICK.finditer(line):
            if not _sid_matches(match.group("sid"), sid):
                continue
            if match.group("outcome") in REAP_REFUSAL_OUTCOMES:
                # The reaper looked at this session and declined. That is evidence it
                # did not reap, so it cannot license a `reaper` cause.
                continue
            return "reaper", line
    return None, None


def find_non_assertion(events_path, sid):
    """Return the first line that names `sid` but does not assert an end, if any."""
    for line in _iter_log_lines(events_path):
        for match in DRAIN_BOUNDARY.finditer(line):
            if _sid_matches(match.group("sid"), sid):
                return line
    return None


def read_ledger(ledger_path, sid):
    """The last transition record for `sid`, or None.

    Read for context only. The ledger writes `reason` as the judgement that the session
    was absent, never the mechanism that removed it, so no cause is derived from it.
    """
    if ledger_path is None:
        return None
    try:
        handle = ledger_path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = None
    with handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if record.get("kind") != "transition":
                continue
            if not _sid_matches(record.get("sid"), sid):
                continue
            found = record
    return found


def resolve(sid, sessions_root, ledger_path):
    events_path = pathlib.Path(sessions_root) / sid / SESSION_EVENTS_NAME
    cause, line = find_assertion(events_path, sid)
    ledger = read_ledger(ledger_path, sid)

    if cause:
        return {
            "sid": sid,
            "verdict": "asserted",
            "cause": cause,
            "line": line.strip(),
            "reason": None,
            "ledger": ledger,
            "events_path": str(events_path),
        }

    named_but_not_asserted = find_non_assertion(events_path, sid)
    if named_but_not_asserted:
        reason = (
            "names-but-does-not-assert: %r names the session but describes a turn "
            "boundary, not an end" % named_but_not_asserted.strip()
        )
    elif not events_path.exists():
        reason = (
            "no own log at %s — the session directory is absent (reaped or never written)"
            % events_path
        )
    else:
        reason = "no line in the session's own log names it and states an action"

    return {
        "sid": sid,
        "verdict": "unverified",
        "cause": None,
        "line": None,
        "reason": reason,
        "ledger": ledger,
        "events_path": str(events_path),
    }


def render(result):
    lines = []
    if result["verdict"] == "asserted":
        lines.append("asserted: %s" % result["cause"])
        lines.append("line: %s" % result["line"])
    else:
        lines.append("unverified: %s" % result["reason"])
    lines.append("session: %s" % result["sid"])
    ledger = result.get("ledger")
    if ledger:
        lines.append(
            "ledger: %s/%s at %s"
            % (ledger.get("state"), ledger.get("reason"), ledger.get("recorded_at"))
        )
    else:
        lines.append("ledger: no transition record")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve one session's cause-of-end from its own log, or say unverified."
    )
    parser.add_argument("--session", required=True, help="the session id to resolve")
    parser.add_argument(
        "--sessions-root",
        default=str(_default_sessions_root()),
        help="directory holding one subdirectory per session (fixture override)",
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help="state ledger file; defaults to today's (fixture override)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    ledger_path = pathlib.Path(args.ledger) if args.ledger else _default_ledger()
    result = resolve(args.session, args.sessions_root, ledger_path)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
