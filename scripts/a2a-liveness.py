#!/usr/bin/env python3
"""Is a remote (docker/k8s) worker's row LIVE, FINISHED or ORPHANED? One instrument, one answer.

The sweep's liveness verdict for a remote row already exists and this file does not replace it:
the attention store's session-heartbeat endpoint carries remote workers (`source a2a`), so
`session-liveness.py --check <id>` answers LIVE for a live one. Measured 2026-10-17 against a
live k8s worker: `LIVE — heartbeat age 1s  source a2a`.

What that instrument cannot answer is the NOT-LIVE case. A deliberately stopped remote worker
and a dead one both read `ABSENT`, so the sweep sees an ended worker as an unowned
`in_progress` row — an orphan candidate — and the drive leg's recovery path would
stop+recreate it, resurrecting work someone ended on purpose and burning a fleet slot.

This script supplies the missing discriminator: the stop the supervisor recorded when
`stop_agent` ran. Read from the ledger, which the attention store already serves.

  --task <task-file>    LIVE (0) / FINISHED (1) / ORPHAN (2) / UNVERIFIED (3) / NOT-REMOTE (4)

⚠️ **UNVERIFIED is a fourth answer, not a flavour of ORPHAN.** An unreadable ledger, a
non-array body, or a liveness probe that could not run means the question was not answered —
and a caller that folds that into "orphan" turns a store outage into permission to
stop+recreate a live worker. Exit 3 is deliberately distinct from exit 2, the same rule
`session-liveness.py` keeps between UNKNOWN and ABSENT.

⚠️ **ORPHAN is licensed only by a READABLE ledger with no stop recorded AND a liveness probe
that positively reported every id not-live.** Nothing else may reach it.

⚠️ **NOT-REMOTE is not a failure.** A row whose target is not docker/k8s is not this rule's
business, and the local classification stands unchanged; the caller must be able to tell that
from UNVERIFIED, or every local row would be reported as a probe that failed.

⚠️ **The stop marker is a field of its own, never the ledger's `status`.** `session-ledger.mjs`
records that 1480 legacy rows still say `running` for workers long dead — a ledger row is
permanent by contract, so filtering on `status` "would resurrect the whole fleet".

⚠️ **The liveness half is REUSED, never re-derived.** `session-liveness.py` is the shipped
instrument and this file shells out to it; a second implementation of "is this id live" is
exactly the drift the plugin's single-home rule forbids.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The store's base URL: `--endpoint`, then `$ATTENTION_STORE_URL`, then localhost — the same
# resolution `session-liveness.py` and `who-needs-me.py` carry, so a store moved once moves
# everywhere and this file gains no second convention to drift.
ENDPOINT_DEFAULT = "http://localhost:18080"
LEDGER_PATH = "/api/1.0/session-ledger"

# The A2A spawn targets. A row carrying one of these is the row this rule owns.
REMOTE_TARGETS = ("docker", "k8s")

# The field `stop_agent` writes. Named here once; the reader and the writer must agree.
#
# ⚠️ **The ledger row is the ONLY stop source since 2026-10-17.** A machine-wide stop file used to
# be read beside it, because `writeLedger` refused every write on an agent adopted from another
# server: a stop on a worker this server did not spawn reached no durable store at all, and the
# file carried exactly that case. The supervisor now records such a stop through the endpoint's
# PATCH, which MERGES where `Post` would blank the owner's fields, so the row is written for every
# stop and the file's whole reason is gone.
#
# ⚠️ **The property the file also carried survives without it.** It was keyed on `worker_id` so a
# task respawned after a stop would not read its SUCCESSOR as stopped. This resolver matches rows
# by `session_id` against the task's CURRENT ids, and a respawn is a new session with a new row —
# so a stale stop is on a row nothing here selects, which is the same outcome by a different
# route. That is why dropping the file is a simplification rather than a lost guarantee.
STOP_FIELD = "stopped_at"

TIMEOUT_SECONDS = 10

# Verdicts, in the order the docstring lists them.
LIVE = "LIVE"
FINISHED = "FINISHED"
ORPHAN = "ORPHAN"
UNVERIFIED = "UNVERIFIED"
NOT_REMOTE = "NOT-REMOTE"

EXIT = {LIVE: 0, FINISHED: 1, ORPHAN: 2, UNVERIFIED: 3, NOT_REMOTE: 4}

# `session-liveness.py`'s own codes, mirrored so a change there fails here rather than
# silently re-mapping. LIVE 0 / ABSENT 1 / UNKNOWN 2 / AMBIGUOUS 3 / MALFORMED 4.
LIVENESS_LIVE = 0
LIVENESS_ABSENT = 1

# An ISO-8601 stamp, date-only or with a time. Deliberately loose on the time half — the
# writer's format is the writer's business — and strict enough that a bool, a number or a
# dict is refused rather than read as a stamp.
ISO_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def endpoint_for(explicit: str | None) -> str:
    """The store's base URL, resolved the way every other reader in this plugin resolves it."""
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("ATTENTION_STORE_URL")
    if env:
        return env.rstrip("/")
    return ENDPOINT_DEFAULT


def parse_ledger_rows(body: object) -> list | None:
    """The ledger's rows, or `None` when the read did not run.

    ⚠️ `None` is "could not read it" and is never folded into `[]`. An empty list is a
    positive claim that the fleet has spawned nothing; a body that is not an array is the
    store answering a different question. Collapsing the two turns an outage into a
    confident empty ledger, which is the direction that hides live workers.
    """
    if not isinstance(body, list):
        return None
    return [row for row in body if isinstance(row, dict)]


def read_ledger(endpoint: str, timeout: float = TIMEOUT_SECONDS) -> list | None:
    """Every row the store serves, or `None` when the read could not run."""
    url = f"{endpoint}{LEDGER_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return parse_ledger_rows(body)


def session_ids_for(task_file: str) -> list[str] | None:
    """The task's whole id set, read by the shipped reader, or `None` when it could not run.

    Reused rather than re-derived: `session-ids.py` reads exactly the two places that hold a
    session id and refuses a `task_identifier` rather than returning it, so neither a bare-uuid
    shape match nor a line-anchored match is reachable through it.
    """
    script = HERE / "session-ids.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--task", task_file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return ids or None


def liveness_of(session_id: str) -> int | None:
    """`session-liveness.py`'s exit code for one id, or `None` when the probe could not run."""
    script = HERE / "session-liveness.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--check", session_id],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode


def remote_rows(rows: list, ids: list[str]) -> list:
    """The ledger rows matching this task's ids that name a docker/k8s target."""
    wanted = set(ids)
    return [
        row
        for row in rows
        if row.get("session_id") in wanted and row.get("target") in REMOTE_TARGETS
    ]


def stopped_state(row: dict) -> str:
    """`stopped` / `none` / `malformed` for one ledger row, from the row's own `stopped_at`.

    ⚠️ **One source, where there used to be two.** The row is written for every stop now: the
    supervisor routes a stop on a worker it did not spawn through the endpoint's PATCH, which
    MERGES, so `writeLedger`'s old blanket refusal of an adopted row is gone and the machine-wide
    stop file this consulted beside the row is gone with it — along with the `worker_id` join that
    existed only to read it.

    ⚠️ **A malformed stamp is refused rather than read as a stop.** `True`, `0` and `{}` are all
    truthy-or-present, and reading any of them as "deliberately stopped" is the direction that
    reports a dead worker as finished and suppresses its recovery.
    """
    if STOP_FIELD not in row:
        return "none"
    value = row.get(STOP_FIELD)
    if value is None or value == "":
        return "none"
    if not isinstance(value, str) or not ISO_STAMP.match(value):
        return "malformed"
    return "stopped"


def verdict(rows: list | None, ids: list[str], liveness: dict) -> str:
    """The verdict for one task's row, from the ledger and the per-id liveness codes.

    `liveness` maps id -> exit code (or `None` when that probe could not run).

    ⚠️ **The decision order is the whole of the safety argument, and each step is placed by the
    direction it fails in.** LIVE is checked FIRST: a stop record is durable, so a task stopped
    and later re-spawned carries a stale stop beside a live new worker, and letting the stop win
    would report a running task as finished. The stop is then checked BEFORE orphan, because
    ORPHAN is the one verdict that authorises stop+recreate — reading a deliberately stopped
    worker as an orphan resurrects work someone ended on purpose, and that is the defect this
    file exists to fix. Every remaining path fails to UNVERIFIED, never to ORPHAN.
    """
    if rows is None or not ids:
        return UNVERIFIED

    remote = remote_rows(rows, ids)
    if not remote:
        return NOT_REMOTE

    codes = [liveness.get(one) for one in ids]
    if any(code is None for code in codes):
        return UNVERIFIED
    if LIVENESS_LIVE in codes:
        return LIVE

    states = {stopped_state(row) for row in remote}
    if "malformed" in states:
        return UNVERIFIED
    if "stopped" in states:
        return FINISHED
    if all(code == LIVENESS_ABSENT for code in codes):
        return ORPHAN
    # UNKNOWN (2) / AMBIGUOUS (3) / MALFORMED (4) — the probe ran but did not answer
    # not-live, and an unreadable source cannot prove a worker dead.
    return UNVERIFIED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", required=True, metavar="TASK_FILE", help="the task file whose row to resolve")
    parser.add_argument("--endpoint", help=f"the attention store's base URL (default {ENDPOINT_DEFAULT})")
    args = parser.parse_args(argv)

    ids = session_ids_for(args.task)
    if not ids:
        print(f"{UNVERIFIED} — no session id could be read from {args.task}")
        return EXIT[UNVERIFIED]

    rows = read_ledger(endpoint_for(args.endpoint))
    liveness = {one: liveness_of(one) for one in ids}
    result = verdict(rows, ids, liveness)
    print(f"{result} — {args.task}")
    return EXIT[result]


if __name__ == "__main__":
    sys.exit(main())
