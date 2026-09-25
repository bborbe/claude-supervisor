#!/usr/bin/env python3
"""Fail when a new-worker spawn was recorded without a mode decision.

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

**Resume rows are excluded.** A resume carries `resumed_from`, and passes
`interactive=false` for a different reason; it answers to the auto-resume gate,
not to the mode decision. Counting it here would fail every fleet that resumed a
worker.

Exit 0 when every new-worker record carries a decided mode, 1 otherwise, naming
each offending record.
"""
import argparse
import glob
import json
import os
import sys

#: Where `server/ledger.mjs` writes, mirroring `scripts/ledger-drill.py`.
DEFAULT_LEDGER = "~/.local/state/claude-supervisor/sessions"

#: The only source that proves the site decided. Everything else is a fallback.
DECIDED = "argument"


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


def offenders(recs):
    """New-worker records whose mode was never decided.

    A record with no `resumed_from` is a new worker; one that has it is a resume.
    `mode_source` absent is NOT an offence — a record written before the field
    existed says nothing about how it was decided, and failing it would make this
    check unusable against any ledger older than the field.
    """
    for path, record in recs:
        if record.get("resumed_from"):
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
    args = parser.parse_args(argv)

    dir = os.path.expanduser(args.ledger)
    if not os.path.isdir(dir):
        print(f"no ledger at {dir} — nothing to check")
        return 0

    recs = list(records(dir))
    bad = list(offenders(recs))

    if not bad:
        decided = sum(
            1 for _, r in recs if not r.get("resumed_from") and r.get("mode_source") == DECIDED
        )
        print(f"OK: {len(recs)} records, {decided} new-worker spawn(s) with a decided mode")
        return 0

    print(f"FAIL: {len(bad)} new-worker spawn(s) with no mode decision")
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
