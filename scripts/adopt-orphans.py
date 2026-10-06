#!/usr/bin/env python3
"""Adopt the live workers an exited manager left behind, so their gates have one owner.

When a manager exits, the workers it spawned do not. `gate-owner-filter.py` resolves
ownership from the spawn edge, so those workers keep a `parent_session` pointing at a
session that is gone: hop 3 still reads it as a manager, hop 4 finds it absent from the
registry, and the pane is emitted `dead-manager` -- to EVERY manager's watcher, because a
dead manager's panes are deliberately kept rather than dropped. Nobody owns them, and
everybody is woken by them.

Measured 2026-10-06 20:27-21:01: spawner `b34530bd` (exited) left panes 192 and 188, and
spawner `f37d6f6a` (exited) left pane 283. Attention Manager and UI Manager each reported
the same gates independently -- duplicate escalation of gates no one had taken on.

This script is the missing step: it finds those workers on a fleet tick and records an
adoption, so the filter's claim arm can route each gate to exactly one manager.

⚠️ **Adoption is a CLAIM, never a re-parent.** `parent_session` is not rewritten. The
filter's own header records why: re-pointing it falsifies provenance, and while both
managers are live it moves the blindness to the other session. The claim store
(`ownership-claim.py`) is the record, and the filter already reads it.

⚠️ **The adopter is resolved by subject, with the tick's own session as the fallback.**
The rule, decided 2026-10-06: a same-subject live successor manager if exactly one
exists, else the session running the tick (the Fleet Manager, on a fleet tick). Subject
comes from `~/.claude/state/worker-manager/<sid>.json`, written by the recording block in
`docs/subject-resolution.md` § Recording.

  * Exactly one live session recording the exited manager's subject adopts.
  * Zero matches, MORE THAN ONE match, or no subject file at all fall back. Ambiguity
    falls back rather than picking: two managers sharing a subject are two managers whose
    gates this script cannot tell apart, and guessing would silence one of them.
  * The no-subject-file case is common, not exotic -- a manager that armed no loop writes
    none. Measured against the two managers in the incident above: `b34530bd` has a
    subject file (`BRO-21389 MDM via REST`), `f37d6f6a` has none. Both branches are real.

⚠️ **Liveness is registry ∪ heartbeat, and the worker must be LIVE.** The registry is
pid-keyed and cannot see a headless in-process worker; the heartbeat store covers exactly
those. Reading either alone answers `0` or misses a population -- `worker-sessions.py`
carries the measurement. This script builds the same union from the same primitives
(`session-liveness.py`) rather than calling `live_workers()`, because that function also
excludes auto-resumes, and that exclusion belongs to the fleet's spawn-target count: an
auto-resumed worker whose manager exited is stranded like any other.

⚠️ **The ledger's own `status` is not a liveness source.** Measured 2026-10-01: 824 of
1075 entries read `running` against 26 live registry sessions. Neither is `ended_at`,
which 179 of 1392 records carry -- the registry and the heartbeat store are the channels.

Storage: claims are written through `ownership-claim.py`, never here. Its `claim` is the
single writer, and its lock is what makes "claimed exactly once" true when two managers
tick at the same moment.

Exit codes: 0 on a completed read (including "nothing to adopt"), 1 when a store the
decision depends on could not be read -- an unreadable registry is NOT an empty one, and
adopting against it would move ownership on evidence nobody has.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# The same override `inbox.py` honours, so a reader pointed at a fixture directory and the
# writer that fills it cannot drift.
SUBJECTS = os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR") or os.path.expanduser(
    "~/.claude/state/worker-manager"
)

OWNERSHIP_SCRIPT = os.path.join(_HERE, "ownership-claim.py")

# Exit code `ownership-claim.py claim` returns when a LIVE manager already holds the
# session. Not an error: it is the single-owner guarantee doing its job.
HELD = 3


def _load(module_name, filename):
    """Import a sibling script by path. They are shipped side by side, not installed."""
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The filter owns `is_manager` and the ledger/claims readers. Imported rather than
# reimplemented: two copies of "what is a manager" would drift, and the drift would be
# silent -- this script would adopt on a predicate the router does not use.
gf = _load("gate_owner_filter", "gate-owner-filter.py")


def normalise(text):
    """Fold a subject for comparison.

    Casefolded and whitespace-collapsed, because the store's subjects are free text
    written by hand: `Personal` and `personal` are the same subject, and the filter's
    header records the same unnormalized-subject problem as the reason it refused to
    scope itself by topic. Here the cost of a miss is a fallback, not a wrong verdict.
    """
    return " ".join((text or "").split()).casefold()


def subject_of(session_id, subjects_dir=SUBJECTS):
    """The normalized subject `session_id` recorded, or None when it recorded none."""
    if not session_id:
        return None
    try:
        with open(os.path.join(subjects_dir, session_id + ".json"), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return normalise(record.get("subject")) or None


def live_session_ids(registry_dir=None, heartbeat_dir=None):
    """Registry ∪ heartbeat as a set of session ids, or None when a store is unreadable.

    None is not "nothing is live" -- it is unknown, and the caller must refuse rather than
    adopt. A missing registry would otherwise read every manager as exited and hand its
    workers to the fallback, which is a silent ownership move on no evidence.
    """
    liveness = _load("session_liveness", "session-liveness.py")
    registry = liveness.read_registry(registry_dir) if registry_dir else liveness.read_registry()
    if registry is None:
        return None
    heartbeats = liveness.read_heartbeats(heartbeat_dir)
    if heartbeats is None:
        return None
    live = set(registry)
    for stamp in heartbeats:
        session_id = stamp.get("session_id")
        # `state` defaults to `live` for a stamp written before the field existed;
        # `unknown` is a cluster stamp whose own store could not be read, which is
        # "cannot tell" and never liveness.
        if session_id and stamp.get("state", "live") == "live":
            live.add(session_id)
    return live


def stranded_workers(ledger, live):
    """{exited manager sid: [worker sid, ...]} for LIVE workers whose spawner is gone.

    The worker must be live: a worker that also exited holds no gate, so claiming it would
    inflate the count with sessions nothing routes to. The spawner must be a manager that
    is not live -- `is_manager` is imported from the filter rather than reimplemented, so
    the two cannot disagree about what a manager is.
    """
    out = {}
    for session_id, record in ledger.items():
        if session_id not in live:
            continue
        parent = record.get("parent_session")
        if not parent or parent in live:
            continue
        if not gf.is_manager(parent, ledger):
            continue
        out.setdefault(parent, []).append(session_id)
    return {parent: sorted(workers) for parent, workers in out.items()}


def already_owned(worker, claims, live):
    """True when a LIVE manager already holds this worker -- the adoption has happened.

    ⚠️ **Without this, every later tick re-reports the same adoption forever.** The
    stranded set is derived from the spawn edge, which does not change when a worker is
    adopted: the manager stays exited, so the worker keeps reading as stranded and the
    tick keeps printing its exit line and a zero adoption count. Measured cost of that
    shape elsewhere in this plugin is the same one the filter was built for -- a line
    that repeats every round stops being read, and the round it matters reads like the
    rounds it did not. A claim held by a manager that is GONE is not ownership (the
    filter fails open on a dead holder for the same reason), so the holder must be live.
    """
    holder = claims.get(worker)
    return bool(holder) and holder in live


def resolve_adopter(exited, live, self_id, subjects_dir=SUBJECTS):
    """(adopter sid, how) for one exited manager -- successor when unique, else fallback."""
    subject = subject_of(exited, subjects_dir)
    if subject:
        matches = [
            session_id
            for session_id in sorted(live)
            if session_id != exited and subject_of(session_id, subjects_dir) == subject
        ]
        if len(matches) == 1:
            return matches[0], "successor"
    return self_id, "fallback"


def claim(session_id, manager, note, claims_file=None):
    """(exit code, output) from the single claim writer. Never writes the store itself."""
    env = dict(os.environ)
    if claims_file:
        env["SUPERVISOR_OWNERSHIP_CLAIMS"] = claims_file
    proc = subprocess.run(
        [
            sys.executable,
            OWNERSHIP_SCRIPT,
            "claim",
            "--session",
            session_id,
            "--manager",
            manager,
            "--note",
            note,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def sid8(session_id):
    return (session_id or "")[:8]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Adopt the live workers an exited manager left behind."
    )
    parser.add_argument(
        "--self",
        dest="self_id",
        default=None,
        help="this tick's session id; the fallback adopter (defaults to CLAUDE_CODE_SESSION_ID)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report, claim nothing")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of lines")
    parser.add_argument("--ledger-dir", default=None)
    parser.add_argument("--registry-dir", default=None)
    parser.add_argument("--heartbeat-dir", default=None)
    parser.add_argument("--subjects-dir", default=SUBJECTS)
    parser.add_argument("--claims-file", default=None)
    args = parser.parse_args(argv)

    self_id = (
        args.self_id
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not self_id:
        sys.stderr.write(
            "adopt-orphans: no tick session id. Pass --self <session-id>; the fallback "
            "adopter is the session running the tick.\n"
        )
        return 1

    ledger_dir = args.ledger_dir or gf.LEDGER
    if not os.path.isdir(ledger_dir):
        # `load_ledger` returns {} for an unreadable directory AND for a genuinely empty
        # one, and the filter's own docstring requires the caller to treat {} as UNKNOWN
        # rather than as "no spawns". The two are told apart here, on the directory, so an
        # absent ledger exits 1 like an unreadable registry instead of reporting a clean
        # round it never measured. A ledger that exists and is empty still exits 0 -- that
        # one is a real answer.
        sys.stderr.write(
            "adopt-orphans: spawn ledger directory unreadable at %s -- adopting nothing.\n"
            % ledger_dir
        )
        return 1
    ledger = gf.load_ledger(ledger_dir)

    live = live_session_ids(args.registry_dir, args.heartbeat_dir)
    if live is None:
        sys.stderr.write(
            "adopt-orphans: registry or heartbeat store unreadable -- liveness is unknown, "
            "adopting nothing rather than moving ownership on no evidence.\n"
        )
        return 1

    claims = gf.load_claims(args.claims_file) if args.claims_file else gf.load_claims()

    # Drop the workers a live manager already holds, and then the managers left with
    # nothing to do. A row that survives is one this round actually acts on.
    stranded = {
        exited: [w for w in workers if not already_owned(w, claims, live)]
        for exited, workers in stranded_workers(ledger, live).items()
    }
    stranded = {exited: workers for exited, workers in stranded.items() if workers}

    rows = []
    for exited in sorted(stranded):
        workers = stranded[exited]
        adopter, how = resolve_adopter(exited, live, self_id, args.subjects_dir)
        adopted, held = [], []
        for worker in workers:
            if args.dry_run:
                adopted.append(worker)
                continue
            code, _ = claim(
                worker,
                adopter,
                "adopted from exited manager %s" % sid8(exited),
                args.claims_file,
            )
            if code == HELD:
                held.append(worker)
            elif code == 0:
                adopted.append(worker)
            else:
                sys.stderr.write(
                    "adopt-orphans: claim failed for %s (exit %d)\n" % (sid8(worker), code)
                )
        rows.append(
            {
                "exited": exited,
                "adopter": adopter,
                "how": how,
                "workers": workers,
                "adopted": adopted,
                "held": held,
            }
        )

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
        return 0

    for row in rows:
        print("manager %s exited" % sid8(row["exited"]))
        print(
            "%d workers adopted from exited manager %s"
            % (len(row["adopted"]), sid8(row["exited"]))
        )
        # ⚠️ The adopter and WHICH BRANCH resolved it are on their own line, never folded
        # into the count line above: that line's format is pinned by the criterion, and a
        # reader matching it should not have to account for a suffix. Without this the
        # round cannot tell whether a same-subject successor took the workers or this
        # session did -- which is the adoption rule's headline decision, and the one thing
        # an operator cannot recover from the claim store without a second command.
        print("  adopted by %s (%s)" % (sid8(row["adopter"]), row["how"]))
        if row["held"]:
            # A held claim is the single-owner guarantee, not a failure -- but it is also
            # not this round's adoption, so it is reported apart from the count above.
            print(
                "%d workers already held by a live manager (exited manager %s)"
                % (len(row["held"]), sid8(row["exited"]))
            )

    # Loud on the count, so an empty run reads as "nothing stranded" rather than as a
    # script that never matched -- the same reason `gate-owner-filter.py` prints its
    # verdict census.
    sys.stderr.write(
        "adopt-orphans: exited managers with live workers: %d  adopted: %d  held: %d\n"
        % (
            len(rows),
            sum(len(r["adopted"]) for r in rows),
            sum(len(r["held"]) for r in rows),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
