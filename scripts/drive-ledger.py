#!/usr/bin/env python3
"""The fleet-drive ledger: which session was nudged, reaped, or found unverifiable.

Storage: ~/.claude/state/fleet-drive/ledger.json -- ONE file, written by this script
and by nothing else (override SUPERVISOR_DRIVE_LEDGER, which is also what lets the
tests exercise merge against a tmp file instead of the operator's real ledger).

Why this exists (measured 2026-10-12): the drive leg used to merge this file with a
hand-written jq expression inside `agents/fleet-drive.md` step 7. A precedence bug in
one round collapsed 376 entries to 1, and the re-nudge/re-reap suppression history --
the whole reason the file exists -- went with it. The prose already said "Merge, never
replace" and had said it for months; prose has nothing to run against it. The merge
now lives here, where `scripts/tests/test_drive_ledger.py` can fail it.

TWO SHAPES, and the live file has carried both at once. The canonical shape is
`{"round": "<ts>", "entries": [...]}`, one entry per session keyed on `session_id`.
A LEGACY shape puts entries at the TOP LEVEL keyed by an 8-char sid, carrying a `sid`
field equal to its own key, never `suppression`, and `open_boxes` on only SOME rows.
Measured 2026-10-12 14:13: 16 flat sid keys -- 3 of them also carrying `open_boxes` --
AND 5 `entries` rows, 3 sids present in both, 18 distinct sessions. The flat shape is
therefore not internally uniform either, so normalisation assumes no fixed field set
on either side; `read` normalises all of it to the canonical shape and `merge` writes
only the canonical shape.

⚠️ A corrupt ledger is REFUSED, not read as empty -- the one place this file
deliberately diverges from its sibling `scripts/asked-ledger.py`, which degrades to an
empty ledger on an unreadable file. Here a false "empty" is the exact regression that
lost 37 entries on 2026-09-23 (the incident `agents/fleet-drive.md` step 3 records), so
`read` exits 2 on a file it cannot parse and `merge` refuses to overwrite one. The
`.bak` is the forensic copy for whoever has to look.

Locking: fcntl.flock on a sidecar lock held across the read-modify-write, then a
pid-suffixed tmp and `os.replace`. A by-hand `/supervisor:fleet-drive` can overlap the
loop's round, and two writers sharing one fixed tmp path is part of what made the wipe
silent -- hence the pid suffix rather than the `ledger.json.tmp` the prose named.

Subcommands: read | merge
"""
import argparse
import fcntl
import json
import os
import shutil
import sys

LEDGER = os.environ.get("SUPERVISOR_DRIVE_LEDGER") or os.path.expanduser(
    "~/.claude/state/fleet-drive/ledger.json"
)

#: Exit code for a ledger this script will not touch: unparseable on disk, or a round
#: payload that is not usable. Distinct from 1 so a caller can tell "refused" from
#: "crashed" -- a refused round wrote nothing, a crashed one may not have.
REFUSED = 2

#: The canonical entry shape, in the order `agents/fleet-drive.md` step 7 names it.
FIELDS = (
    "session_id",
    "name",
    "verdict",
    "open_boxes",
    "probe",
    "nudge",
    "suppression",
    "task_file",
    "reprobe_verdict",
    "reprobe_probe",
    "unverifiable_streak",
    "unverifiable_round",
    "unverifiable_action",
)
VERDICTS = ("revive", "blocked", "unverifiable", "finished")
#: What the 2-round rule did about a row, or "" when it has not acted. The marker is what
#: makes the action fire ONCE per silence rather than on every round at or past the
#: threshold: a row held back by the per-round cap keeps advancing its streak, so a
#: `streak == 2` test would never match it again and it would be acted on never.
UNVERIFIABLE_ACTIONS = ("", "nudge", "escalation")
NUDGES = ("drafted", "none")

#: Top-level keys that are not themselves session entries.
RESERVED = ("round", "entries")


class Corrupt(Exception):
    """The ledger exists but cannot be parsed. Never treated as an empty ledger."""


def lock_path():
    """Derived, never cached at import: the tests point LEDGER at a tmp file."""
    return LEDGER + ".lock"


def bak_path():
    return LEDGER + ".bak"


def canonical(sid, raw):
    """One entry, canonical shape, all twelve fields present.

    `open_boxes` is deliberately NOT defaulted to 0. A legacy row never carried the
    field, and 0 means "verified finished" while None means "never counted" -- the
    suppression rule compares this value across rounds, so conflating them would let
    a legacy row read as a verified-finished one.

    `task_file` defaults to "" for the same reason `name` does: an entry written before
    the field existed must still read as a complete entry, and "" is the honest value
    -- it says "no path recorded". Step 0 of `agents/fleet-drive.md` treats that as
    unverifiable rather than guessing one, because neither other identity field can
    resolve a task: `session_id` is an 8-char display handle and `name` is a session
    name that need not equal the filename (measured 2026-10-20: `Row Whose Body
    Demands…` against `A Row Whose Body Demands…`).

    `unverifiable_streak`/`unverifiable_round` carry how many CONSECUTIVE rounds a
    session has been classified unverifiable, and the round tag at which the count was
    last advanced. They live here rather than in a second file because the ledger is
    already the one store keyed per session that survives a round. Consecutiveness is
    not a field: it is `unverifiable_round` equalling the ledger's own `round`, which
    is the previous round's tag. A gap therefore resets the count to 1 on its own,
    without anything having to notice the gap -- the same shape as the sweep's
    `stall_count`, which resets on the condition rather than on a written zero.
    """
    raw = raw if isinstance(raw, dict) else {}
    return {
        "session_id": sid,
        "name": raw.get("name") or "",
        "verdict": raw.get("verdict") or "unverifiable",
        "open_boxes": raw.get("open_boxes"),
        "probe": raw.get("probe") or "",
        "nudge": raw.get("nudge") or "none",
        "suppression": raw.get("suppression") or "none",
        "task_file": raw.get("task_file") or "",
        # What step 0's re-probe observed, kept apart from the agent's own
        # `verdict`/`probe`. The two speak different vocabularies: the script emits
        # `needs-step-4` for a live row, which is deliberately NOT in VERDICTS because
        # the agent resolves it to revive|blocked|unverifiable after step 4. Comparing
        # the script's observation against the agent's conclusion therefore never
        # matched, and a live row re-signalled every round (measured 2026-10-21:
        # identical PARKED blocks in two consecutive rounds).
        "reprobe_verdict": raw.get("reprobe_verdict") or "",
        "reprobe_probe": raw.get("reprobe_probe") or "",
        "unverifiable_streak": streak_of(raw.get("unverifiable_streak")),
        "unverifiable_round": raw.get("unverifiable_round") or "",
        "unverifiable_action": raw.get("unverifiable_action") or "",
    }


def streak_of(value):
    """The consecutive-unverifiable count, as a non-negative int.

    `bool` is an `int` in Python, so a hand-edited `true` would otherwise read as 1 --
    a plausible-looking count nobody computed. Anything unparseable is 0, and the
    asymmetry is deliberate: the count gates an action at exactly 2, so a corrupt
    value read as 0 stays silent, while a corrupt value read as 2 sends a nudge on no
    evidence. A round payload that carries a bad count is REFUSED by `check()` before
    it ever reaches here, so this default only covers a ledger already on disk.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def collect(data):
    """Every session in the ledger, normalised, keyed on session id.

    Precedence: a canonical `entries` row WINS over a legacy flat row for the same
    sid. The flat row is the older write; the `entries` list is the shape the doc
    prescribes. Measured 2026-10-12: 3 sids sat in both shapes at once.
    """
    out = {}
    if not isinstance(data, dict):
        return out
    for key, raw in data.items():
        if key in RESERVED:
            continue
        out[key] = canonical(key, raw)
    rows = data.get("entries")
    if isinstance(rows, list):
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            sid = raw.get("session_id") or raw.get("sid")
            if sid:
                out[sid] = canonical(sid, raw)
    return out


def ordered(entries):
    """A stable order, so a merge that changes nothing still writes identical bytes."""
    return sorted(entries.values(), key=lambda entry: entry["session_id"])


def load():
    """(data, exists). Raises Corrupt when the file exists but cannot be read.

    The absent/empty case returns `({}, False)`; the unparseable case raises, and the
    two must never be conflated -- that conflation is the 2026-09-23 regression.
    """
    if not os.path.exists(LEDGER):
        return {}, False
    try:
        with open(LEDGER, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise Corrupt(str(exc))
    if not isinstance(data, dict):
        raise Corrupt("top level is %s, not an object" % type(data).__name__)
    return data, True


def write(data):
    """makedirs -> pid-suffixed tmp -> os.replace, tmp unlinked on any failure."""
    parent = os.path.dirname(LEDGER)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = LEDGER + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, LEDGER)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def snapshot():
    """Copy the PRE-merge file aside. False when there is nothing to lose yet.

    Byte copy, not a re-serialisation: the `.bak` has to be the file as it stood, so
    it can be restored verbatim and compared byte-for-byte.
    """
    if not os.path.exists(LEDGER):
        return False
    shutil.copyfile(LEDGER, bak_path())
    return True


class Locked(object):
    """Hold the sidecar lock across the read-modify-write. Overlapping rounds race here."""

    def __enter__(self):
        path = lock_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.handle = open(path, "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        return False


def refuse(message):
    print("drive-ledger: %s" % message, file=sys.stderr)
    return REFUSED


def check(row, sid):
    """None when the row is usable, else the reason it is not."""
    verdict = row.get("verdict")
    if verdict not in VERDICTS:
        return "session %s carries verdict %r, not one of %s" % (
            sid,
            verdict,
            "|".join(VERDICTS),
        )
    nudge = row.get("nudge") or "none"
    if nudge not in NUDGES:
        return "session %s carries nudge %r, not one of %s" % (
            sid,
            nudge,
            "|".join(NUDGES),
        )
    streak = row.get("unverifiable_streak", 0)
    if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
        return "session %s carries unverifiable_streak %r, not a non-negative int" % (
            sid,
            streak,
        )
    action = row.get("unverifiable_action", "")
    if action not in UNVERIFIABLE_ACTIONS:
        return "session %s carries unverifiable_action %r, not one of %r" % (
            sid,
            action,
            UNVERIFIABLE_ACTIONS,
        )
    return None


def incoming(payload):
    """(entries_by_sid, None) or (None, reason). Validates the whole payload first."""
    rows = payload.get("entries")
    if not isinstance(rows, list):
        return None, "round payload carries no `entries` list"
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            return None, "a round entry is %s, not an object" % type(row).__name__
        sid = row.get("session_id") or row.get("sid")
        if not sid:
            return None, "a round entry carries no session_id"
        if sid in out:
            return None, "session %s appears twice in one payload" % sid
        reason = check(row, sid)
        if reason:
            return None, reason
        out[sid] = canonical(sid, row)
    return out, None


def cmd_read(_args):
    try:
        data, _ = load()
    except Corrupt as exc:
        return refuse(
            "%s exists but cannot be parsed (%s) -- refusing to read it as an empty "
            "ledger; an empty read is the regression this guard exists for" % (LEDGER, exc)
        )
    json.dump(
        {"round": data.get("round"), "entries": ordered(collect(data))},
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


def cmd_merge(_args):
    """Merge this round's entries from stdin. The script owns the merge, never the caller.

    The caller supplies only what THIS round saw; every prior entry it does not name
    is kept verbatim. That is the invariant a hand-written merge broke: a round can no
    longer shrink the file by omission, because it never holds the merged set at all.
    """
    try:
        payload = json.loads(sys.stdin.read() or "")
    except ValueError as exc:
        return refuse("round payload is not JSON: %s" % exc)
    if not isinstance(payload, dict):
        return refuse("round payload is %s, not an object" % type(payload).__name__)

    fresh, reason = incoming(payload)
    if reason:
        return refuse("%s -- nothing written" % reason)

    with Locked():
        try:
            data, _ = load()
        except Corrupt as exc:
            return refuse(
                "%s exists but cannot be parsed (%s) -- refusing to overwrite it; the "
                ".bak is the forensic copy" % (LEDGER, exc)
            )
        merged = collect(data)
        merged.update(fresh)
        had_bak = snapshot()
        write({"round": payload.get("round"), "entries": ordered(merged)})

    print(
        "ledger written: %d entries · %s · bak: %s"
        % (len(merged), payload.get("round") or "—", "yes" if had_bak else "no")
    )
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Read and merge the fleet-drive ledger."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser(
        "read", help="print the ledger normalised to one canonical shape"
    )
    read.set_defaults(func=cmd_read)
    merge = sub.add_parser(
        "merge", help="merge this round's entries (stdin JSON) and write the ledger"
    )
    merge.set_defaults(func=cmd_merge)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
