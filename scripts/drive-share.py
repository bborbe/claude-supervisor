#!/usr/bin/env python3
"""The round's share: detected problems that ended in a verified action.

A fleet round reports what it found and what it drafted; it never reports whether any of
it changed anything. Measured 2026-10-22: rounds ended "0 nudges, 0 reaps" and one drive
leg marked 99 of 101 candidates unverifiable and did nothing, and neither fact was visible
in the output. This prints the one number that makes silence visible:

    share: <verified>/<detected> (<pct>%)        or        share: 0/0 (n/a)

WHY IT IS A SCRIPT. The criterion is that the numerator EXCLUDES a row whose action
produced no movement, and a hardcoded ratio satisfies every shape-only check of that. The
seams `transcript_ts()` and `task_mtime()` are what the test stubs: a detected row with an
action taken and no advance must count as unverified, and a markdown file has no callable
seam to stub.

DEFINITIONS, and they are the whole content of the number:

  detected  -- every `CLASSIFICATION` row the digest classed `stalled` or `parked`, every
               row whose class names an error, plus every entry this round classified
               `unverifiable`. Deduplicated by session id, because one session can appear
               in both the digest and the entries.
  verified  -- a detected row where an action was taken AND the session's transcript moved
               past the round start, or its task file changed. "Action taken" is a drafted
               nudge or reap, or the drive leg's own 2-round pane read.

⚠️ **The round start is the comparison point, and it is deliberately a floor rather than
the action's own timestamp.** The action is drafted mid-round and the send follows, so a
per-action timestamp would be more precise -- but a *stalled* row has no movement by
definition and a *parked* row is idle, so movement after the round start is attributable to
the round for exactly the rows this measures. A row already progressing is not in the
detected set at all, so it cannot inflate the numerator.

⚠️ **No `error` class exists in the sweep today** (`agents/fleet-sweep-reader.md` step 8
defines `held`, `stood-down`, `lapsed`, `progressing`, `deferred`, `stalled`, `parked`,
`finished`, `orphan` and `unclassified`). The error clause matches none until one is added,
and it is here so that adding one cannot silently drop those rows out of the denominator.

Subcommands: share
"""
import argparse
import glob
import json
import os
import re
import sys

#: Exit code for an input this script will not compute from. Distinct from 1 (a crash).
REFUSED = 2

#: The digest classes that make a row a detected problem. `unverifiable` is not here: it
#: is a drive-leg verdict and arrives on the entries, not on a digest class.
DETECTED_CLASSES = ("stalled", "parked")

HOME = os.environ.get("HOME") or os.path.expanduser("~")


def projects_dir():
    """Where Claude Code keeps per-session transcripts. `SUPERVISOR_PROJECTS_DIR` overrides.

    The same root `scripts/fleet-sessions.py` reads, and the same override, so the reader
    and this measure cannot resolve two different trees.
    """
    override = os.environ.get("SUPERVISOR_PROJECTS_DIR")
    return os.path.expanduser(override) if override else os.path.join(HOME, ".claude", "projects")


def round_epoch(value):
    """The round tag as epoch seconds (UTC), or None when it is not a round tag.

    Both spellings the fleet writes are accepted: the ISO form with colons and the
    filesystem-sanitized form whose clock separators are dashes
    (`2026-10-22T14-07-35Z`), which is what the payload filename carries. Parsed without
    `time.strptime` so the two shapes need one pattern rather than a try/except ladder.
    """
    match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})[-:](\d{2})[-:](\d{2})Z?$", (value or "").strip()
    )
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    import calendar
    import time

    return calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))


def transcript_ts(sid):
    """The session transcript's last-message timestamp, or None when it cannot be read.

    Newest transcript wins: a resumed session can leave more than one file carrying the
    id, and the live one is the one that moved. `None` is not 0 -- an unreadable
    transcript must never read as "no movement", which is the same three-state rule
    `scripts/live-workers.py` and `scripts/drive-retained.py` already hold.
    """
    if not sid:
        return None
    newest = None
    for path in glob.glob(os.path.join(projects_dir(), "*", "%s*.jsonl" % sid)):
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def task_mtime(path):
    """The task file's mtime, or None when it is absent or unreadable."""
    if not path:
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def digest_classes(path):
    """(name, sid8, class) for every row of the digest's `CLASSIFICATION` section.

    Read as text, because the digest is a render rather than a serialisation: the section
    runs from its heading to the next all-caps heading, and each row is `·`-separated with
    the class at index 3. A digest with no `CLASSIFICATION` section yields nothing and the
    caller reports the refusal -- never an empty classification, which would read as a
    fleet with no problems.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    rows = []
    inside = False
    for line in lines:
        if line.startswith("CLASSIFICATION"):
            inside = True
            continue
        if not inside:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if line and not line[0].isspace():
            break
        parts = [part.strip() for part in stripped.split("·")]
        if len(parts) < 4:
            continue
        label = parts[0]
        match = re.search(r"\[([^\]]+)\]", label)
        sid8 = match.group(1) if match else ""
        name = label[: match.start()].strip() if match else label
        rows.append((name, sid8, parts[3]))
    return rows


def action_taken(entry):
    """Did this round act on the row?

    `nudge: drafted` covers a drafted nudge and a drafted reap -- the ledger records both
    the same way. The 2-round pane read is recorded as a streak, not as a nudge, so it is
    read from the streak field.
    """
    if (entry.get("nudge") or "none") == "drafted":
        return True
    streak = entry.get("unverifiable_streak")
    return isinstance(streak, int) and not isinstance(streak, bool) and streak == 2


def measure(
    digest_rows,
    entries,
    start,
    transcript_ts_fn=transcript_ts,
    task_mtime_fn=task_mtime,
):
    """One row per detected problem, each carrying whether its action verified.

    Returns the list of rows; the caller renders and counts them. Kept pure over its two
    seams so the test can drive a row that took an action and did not move.
    """
    by_sid = {}
    for entry in entries or []:
        sid = entry.get("session_id") or ""
        if sid:
            by_sid[sid] = entry

    def lookup(key):
        """The entry for a row, by full session id or by the digest's 8-char handle.

        The digest carries the handle and the entries carry the whole id, so a
        handle-only lookup would find nothing and every action would read as "no action
        taken" -- which renders as an honest zero rather than as an error, the exact
        shape this script exists to make visible.
        """
        if key in by_sid:
            return by_sid[key]
        for sid, entry in by_sid.items():
            if key and sid.startswith(key):
                return entry
        return None

    detected = {}
    for name, sid8, klass in digest_rows or []:
        key = sid8 or name
        if klass in DETECTED_CLASSES or "error" in klass.lower():
            detected[key] = {"name": name, "sid": sid8, "class": klass, "entry": None}
    for sid, entry in by_sid.items():
        if (entry.get("verdict") or "") != "unverifiable":
            continue
        key = (sid or "")[:8] or entry.get("name") or "?"
        row = detected.setdefault(
            key,
            {
                "name": entry.get("name") or "?",
                "sid": (sid or "")[:8],
                "class": "unverifiable",
                "entry": None,
            },
        )
        row["entry"] = entry
        if row["class"] not in DETECTED_CLASSES:
            row["class"] = "unverifiable"

    out = []
    for key in sorted(detected):
        row = detected[key]
        entry = row["entry"] or lookup(key) or {}
        acted = action_taken(entry)
        evidence = ""
        verified = False
        if not acted:
            evidence = "no action taken"
        else:
            moved = []
            if start is not None:
                stamp = transcript_ts_fn(row["sid"])
                if stamp is not None and stamp > start:
                    moved.append("transcript advanced")
                when = task_mtime_fn((entry.get("task_file") or "").strip())
                if when is not None and when > start:
                    moved.append("task file changed")
            if moved:
                verified = True
                evidence = " + ".join(moved)
            else:
                evidence = "no movement after the round start"
        out.append(
            {
                "name": row["name"],
                "sid": row["sid"],
                "class": row["class"],
                "action": "action" if acted else "no action",
                "verified": verified,
                "evidence": evidence,
            }
        )
    return out


def render(rows):
    """The `SHARE` block plus the one line the round prints.

    One line per detected row, because a bare ratio is exactly what a hardcoded number
    also produces -- the rows are the evidence that the counts were computed.
    """
    lines = ["SHARE — detected problems ending in a verified action"]
    for row in rows:
        lines.append(
            "  %s [%s] · %s · %s · %s · %s"
            % (
                row["name"] or "?",
                row["sid"] or "?",
                row["class"],
                row["action"],
                "verified" if row["verified"] else "not verified",
                row["evidence"],
            )
        )
    if not rows:
        lines.append("  (none)")
    verified = sum(1 for row in rows if row["verified"])
    detected = len(rows)
    if not detected:
        lines.append("share: 0/0 (n/a)")
    else:
        lines.append("share: %d/%d (%.0f%%)" % (verified, detected, 100.0 * verified / detected))
    return "\n".join(lines)


def cmd_share(args):
    rows = digest_classes(args.digest)
    if rows is None:
        print(
            "drive-share: digest unreadable at %s -- refusing to report a share over a "
            "classification that could not be read" % args.digest,
            file=sys.stderr,
        )
        return REFUSED
    entries = []
    if args.entries:
        try:
            with open(args.entries, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            print("drive-share: entries unreadable: %s" % exc, file=sys.stderr)
            return REFUSED
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            print(
                "drive-share: entries payload carries no `entries` list -- nothing measured",
                file=sys.stderr,
            )
            return REFUSED
        entries = payload["entries"]
    start = round_epoch(args.round)
    if args.round and start is None:
        print(
            "drive-share: round tag %r is not a round timestamp -- refusing to measure "
            "against an unknown start" % args.round,
            file=sys.stderr,
        )
        return REFUSED
    print(render(measure(rows, entries, start)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="drive-share.py")
    sub = parser.add_subparsers(dest="command")
    share = sub.add_parser(
        "share", help="print detected problems and the ones that ended in a verified action"
    )
    share.add_argument("--round", required=True, help="this round's timestamp")
    share.add_argument("--digest", required=True, help="the sweep's digest render for this round")
    share.add_argument(
        "--entries",
        default=None,
        help="this round's drive-leg entries, for the unverifiable rows and the actions taken",
    )
    args = parser.parse_args(argv)
    if args.command == "share":
        return cmd_share(args)
    parser.print_help(sys.stderr)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main())
