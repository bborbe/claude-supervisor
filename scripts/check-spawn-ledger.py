#!/usr/bin/env python3
"""Fail when a recent new-worker spawn was recorded without a mode decision.

`mode_source` names which of four sources decided a worker's mode — `argument`,
`env`, `config`, `default`. Only `argument` records that the spawn site *decided*:
the other three are a fallback, and `config` in particular is what a site produces
when it never passed the argument at all.

That matters because `config` is byte-identical whether a site was wired and then
bypassed, or never wired in the first place — so no measurement can tell the two
apart. Measured 2026-09-25: `agent_163` and `agent_164`, spawned from `Manager
Layer`, both reported `mode_source=config` — a direct open by
`agents/manager-drive.md` that skipped the classification entirely, on a plugin
that already carried the prose fix.

**Why this reads the ledger rather than a file.** `scripts/check-spawn-mode.py`
reads what a file *says*; the defect above was defined by what the file's grant
*permitted*, and a text scan cannot see a runtime call. This is the other half:
the record the spawn actually produced.

**Why it is not a precommit gate.** It reads the live ledger at
`~/.local/state/claude-supervisor/sessions/`, which exists only after spawns have
happened. `make precommit` runs against a tree, not against a machine's spawn
history, so this cannot live there — run it by hand, or from a sweep.

**Why there is a time window, and why it is not optional.** The wiring landed
incrementally, so the ledger's *history* is mostly `config` rows: run over the
whole ledger on 2026-09-25 it reported **241** offenders, and a gate that fails on
every historical row can never pass — it would measure nothing and be turned off
within a day. The window is what makes the verdict mean *"this is still happening
now"* rather than *"this once happened"*, and it is the same day-window the parent
task's SC3 uses.

**Resume rows are excluded.** A resume carries `resumed_from`, and passes
`interactive=false` for a different reason; it answers to the auto-resume gate,
not to the mode decision. Counting it here would fail every fleet that resumed a
worker.

Exit 0 when every in-window new-worker record carries a decided mode, 1 otherwise,
naming each offending record.
"""
import argparse
import datetime
import glob
import json
import os
import sys

#: Where `server/ledger.mjs` writes, mirroring `scripts/ledger-drill.py`.
DEFAULT_LEDGER = "~/.local/state/claude-supervisor/sessions"

#: The only source that proves the site decided. Everything else is a fallback.
DECIDED = "argument"

#: Wide enough to cover a working day, narrow enough that the verdict describes
#: current behaviour rather than the ledger's history.
DEFAULT_WINDOW_HOURS = 24


def spawned_at(record):
    """The record's spawn time as an aware datetime, or None when undatable.

    `buildRecord` stamps ISO-8601 with a `Z` suffix; `fromisoformat` only learned to
    accept `Z` in 3.11, so it is normalised here rather than assumed. A record this
    cannot date is reported as skipped by the caller — never silently dropped, and
    never counted as an offence it may be years old.
    """
    raw = record.get("spawned_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive stamp is read as UTC rather than rejected: the writer always emits an
    # offset, so a naive one came from a hand-edited record, not from the server.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def records(dir):
    """Every parseable record. An unreadable or half-written file is skipped rather
    than raised on: the writer renames into place, and a reader that crashed on a
    transient would report a clean ledger as a broken one."""
    for path in sorted(glob.glob(os.path.join(dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                yield path, json.load(handle)
        except (OSError, ValueError):
            continue


def offenders(recs, since):
    """In-window new-worker records whose mode was never decided.

    A record with no `resumed_from` is a new worker; one that has it is a resume.
    `mode_source` absent is NOT an offence — a record written before the field
    existed says nothing about how it was decided, and failing it would make this
    check unusable against any ledger older than the field.
    """
    for path, record in recs:
        if record.get("resumed_from"):
            continue
        at = spawned_at(record)
        if at is None or at < since:
            continue
        if record.get("mode_source") == "config":
            yield path, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--ledger",
        default=DEFAULT_LEDGER,
        help=f"ledger directory (default: {DEFAULT_LEDGER})",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help=f"how far back to judge (default: {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 instant to treat as now; defaults to the real clock",
    )
    args = parser.parse_args(argv)

    dir = os.path.expanduser(args.ledger)
    if not os.path.isdir(dir):
        print(f"no ledger at {dir} — nothing to check")
        return 0

    now = (
        datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.datetime.now(datetime.timezone.utc)
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    since = now - datetime.timedelta(hours=args.since_hours)

    recs = list(records(dir))
    in_window = [
        (p, r)
        for p, r in recs
        if not r.get("resumed_from") and spawned_at(r) is not None and spawned_at(r) >= since
    ]
    bad = list(offenders(recs, since))
    undated = sum(1 for _, r in recs if spawned_at(r) is None)

    if not bad:
        decided = sum(1 for _, r in in_window if r.get("mode_source") == DECIDED)
        print(
            f"OK: {len(recs)} records, {len(in_window)} new-worker spawn(s) in the last "
            f"{args.since_hours:g}h, {decided} with a decided mode"
        )
        if undated:
            print(f"note: {undated} record(s) carry no parseable spawned_at — not dated, not judged")
        return 0

    print(
        f"FAIL: {len(bad)} new-worker spawn(s) in the last {args.since_hours:g}h "
        "with no mode decision"
    )
    for path, record in bad:
        print(
            f"  {os.path.basename(path)}: mode={record.get('mode')} "
            f"mode_source={record.get('mode_source')} label={record.get('label')!r}"
        )
    print(
        "\nEach row above was spawned without an explicit `interactive=` argument, so "
        "the fleet config decided and the record cannot say whether the site was "
        "wired-and-bypassed or never wired at all."
    )
    if undated:
        print(f"note: {undated} record(s) carry no parseable spawned_at — not dated, not judged")
    return 1


if __name__ == "__main__":
    sys.exit(main())
