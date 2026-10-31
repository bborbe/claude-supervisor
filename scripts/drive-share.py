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
               past the round start, or its task file changed. "Action taken" is, in order
               of authority: membership of the caller's `--sent` set; else a `nudge:
               drafted`; else a non-empty `unverifiable_action` on the ledger. A streak is
               never the test — the drive leg acts at `>= 2`, so a row held back by the cap
               acts at 3 and an `== 2` check would call it unactioned.

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

#: Every class a `CLASSIFICATION` row can carry. Used to LOCATE the class column instead
#: of assuming its index -- see `digest_rows`. `deferred` and `orphan` are here because
#: `agents/fleet-sweep-reader.md` declares them even though neither is a detected problem.
DIGEST_CLASSES = (
    "progressing",
    "stalled",
    "parked",
    "finished",
    "unclassified",
    "held",
    "stood-down",
    "lapsed",
    "done",
    "blocked",
    "error",
    "error-marked",
    "deferred",
    "orphan",
)


def class_token(cell):
    """The class word inside a cell, ignoring a qualifier or a decoration.

    The render qualifies a class inline -- `parked (closer pick)`, `finished (shell child
    held)`, `⏸️ HELD — <reason>` -- so an exact match against `DIGEST_CLASSES` misses real
    rows. Only the leading word is the class. Measured 2026-10-22: `parked (closer pick)`
    is a parked row that an exact match dropped from the denominator.
    """
    match = re.match(r"[^a-z]*([a-z][a-z-]*)", cell.lower())
    return match.group(1) if match else ""

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


def row_cells(text):
    """The cells of one row after its `[sid8]`: `·`-separated when the render uses them.

    Two renders are live at once, and they are not one shape with an optional column --
    they differ in the separator itself. Measured 2026-10-29 across consecutive rounds:

        `⚙ exactly-once-fix [79c6d892] · shell · 9h · parked · Exactly-Once Still Fails…`
        `Agents Spawn and Finish Withou [c328d8e3] waiting 6h lapsed — b— http://…`

    The second truncates the name and drops every `·`, so a parser that only splits on `·`
    gets one part, skips the row as too short, and reports **zero** rows *and* zero
    unparsed -- a silent zero that reads exactly like "the fleet has no parked sessions".
    Rounds `2026-10-29T05-29-16Z` and `06-01-14Z` were both that shape.
    """
    if "·" in text:
        return [cell.strip() for cell in text.split("·") if cell.strip()]
    return text.split()


def class_in_cells(cells):
    """The class token among the first three cells after the sid.

    Status and age always precede the class and the task column always follows it, so
    three is the whole window in every observed shape -- with an age, without one, and
    with either separator. Bounded rather than open-ended, because scanning the rest of
    the row walks into the task title and reads `brandnewclass · Parked Ideas.md` as a
    parked row.

    The structural guard survives the render change: **the class cell is never a task
    basename**, so a cell ending `.md` is rejected even when its first word is a class.
    An empty result is the caller's loud path, never a silently dropped row.
    """
    for cell in cells[:3]:
        token = class_token(cell)
        if token in DIGEST_CLASSES and not cell.lower().endswith(".md"):
            return token
    return ""


def classification_present(path):
    """Whether the digest carries a `CLASSIFICATION` heading at all.

    A missing heading is **not** "a fleet with no problems" -- it is a render this parser
    cannot read (a renamed section, a truncated file), and measuring a share over the
    ledger alone would silently drop the digest half of the denominator. An explicitly
    empty section is different: `CLASSIFICATION  UNKNOWN (pass not run)` is a readable
    statement about the round, so it keeps the `0/0 (n/a)` reading rather than refusing.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return any(line.startswith("CLASSIFICATION") for line in handle)
    except OSError:
        return False


def digest_rows(path):
    """(rows, unparsed) for the digest's `CLASSIFICATION` section.

    `rows` is `(name, sid8, class)` for every row whose class token is recognised;
    `unparsed` counts the rows that carried no token in `DIGEST_CLASSES`. The count is
    returned rather than swallowed because **a silently skipped row is the defect this
    function already shipped once**: the original build indexed `parts[3]`, read the task
    file as the class, and dropped every stalled/parked row without a trace. Skipping an
    unrecognised token quietly would reintroduce exactly that failure shape for the next
    class the render adds.

    Read as text, because the digest is a render rather than a serialisation: the section
    runs from its heading to the next all-caps heading, rows are separated by ` || `, and
    a row's own cells are separated by `·` -- or by whitespace alone in the compact
    render, which drops the `·` entirely. See `row_cells`.

    ⚠️ **The class column is LOCATED, never assumed.** The digest alternates between two
    live shapes: `Name [sid8] · idle · parked · task · boxes · link` (class at index 2)
    and `Name [sid8] · idle · 12m · parked · …` (an age column, class at index 3). A fixed
    index therefore reads the task file as the class on one shape and counts **zero**
    stalled/parked rows, which is invisible because the share's denominator is usually
    dominated by the ledger's `unverifiable` entries. Measured 2026-10-22 across three
    consecutive live rounds: the fixed index detected 0, 0 and 1; scanning detected 5, 7
    and 1.

    The separator is a second axis of the same problem, and it failed the same way on
    2026-10-29: two consecutive rounds used the compact render, whose rows carry no `·`,
    so the split produced one part, the row was skipped as too short, and the parser
    returned **0 rows with 0 unparsed** -- a silent zero, indistinguishable from a fleet
    with nothing parked. The share it printed that round was short by exactly the 13
    digest-only parked rows.

    A digest with no `CLASSIFICATION` section yields `([], 0)` here; `cmd_share` refuses
    that case through `classification_present()` rather than reporting a share over the
    ledger alone -- a renamed heading is a render this parser cannot read, not a fleet with
    no problems. An explicitly empty section (`CLASSIFICATION  UNKNOWN (pass not run)`) is
    a readable statement and keeps the `0/0 (n/a)` reading.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    rows = []
    unparsed = 0
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
        # One line can carry several rows: the compact render joins them with ` || `.
        # Splitting on it here is what makes that render readable at all -- without it a
        # three-row line parses as one row and the other two vanish.
        for text in stripped.split(" || "):
            text = text.strip()
            if not text:
                continue
            # A parenthesised line inside the section is a summary, not a session row. It
            # is skipped without counting: `(none)` and `(waiting x6: …)` are prose, not
            # rows, and counting them would make the unparsed figure permanently non-zero
            # and therefore ignorable -- the opposite of loud.
            if text.startswith("("):
                continue
            match = re.search(r"\[([^\]]+)\]", text)
            sid8 = match.group(1) if match else ""
            name = text[: match.start()].strip() if match else text
            # Everything after the sid is the cell stream, in whichever separator this
            # render uses. A row whose first three cells carry no class is counted as
            # unparsed -- never silently dropped, and never satisfied by a task basename.
            klass = class_in_cells(row_cells(text[match.end():] if match else text))
            if not klass:
                unparsed += 1
                continue
            rows.append((name, sid8, klass))
    return rows, unparsed


def digest_classes(path):
    """The parsed rows only -- `digest_rows` without the unparsed count."""
    result = digest_rows(path)
    return None if result is None else result[0]


def action_taken(entry, sid="", sent=None):
    """Did this round act on the row -- and, when known, did the action actually leave?

    ⚠️ **The drive leg DRAFTS; the round SENDS.** `nudge: drafted` on a ledger entry means
    a message was composed, not that it reached anyone -- `agents/fleet-drive.md` says so
    outright ("A `drafted` entry counts as nudged next round — if the caller's send fails,
    the next round suppresses rather than nags"). Counting a draft as an action therefore
    credits the numerator for something that never left the building. Measured
    2026-10-22T13:15Z: a round with every send deliberately held still printed
    `share: 2/112 (2%)`, because two drafted rows happened to move for unrelated reasons.

    So when the caller passes `sent` -- the session ids it actually sent to or posted a
    card for -- that set is the only authority. `sent=None` means the caller could not say,
    and the draft stands in, with the block's header saying so rather than the number
    quietly overstating.
    """
    if sent is not None:
        return bool(sid) and sid in sent
    if (entry.get("nudge") or "none") == "drafted":
        return True
    # The ledger's own marker, never the streak: under the `>= 2` trigger a row held back
    # by the per-round cap acts at 3, 4, … and an `== 2` test would read it as "no action
    # taken" while a nudge or escalation was in fact taken.
    return bool(entry.get("unverifiable_action"))


def measure(
    classes,
    entries,
    start,
    transcript_ts_fn=transcript_ts,
    task_mtime_fn=task_mtime,
    sent=None,
):
    """One row per detected problem, each carrying whether its action verified.

    Returns the list of rows; the caller renders and counts them. Kept pure over its two
    seams so the test can drive a row that took an action and did not move. `sent` is the
    set of session ids the round actually sent to or posted a card for — see
    `action_taken()` for why a draft is not the same thing.
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
    for name, sid8, klass in classes or []:
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
        acted = action_taken(entry, row["sid"], sent)
        evidence = ""
        verified = False
        if not acted:
            drafted = (entry.get("nudge") or "none") == "drafted" or bool(
                entry.get("unverifiable_action")
            )
            # Naming the difference matters: "no action taken" and "drafted but never
            # sent" both leave the numerator at zero, and only the second is a defect
            # somebody should fix.
            evidence = (
                "drafted, not sent"
                if (sent is not None and drafted)
                else "no action taken"
            )
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


def render(rows, sent_known=True, unparsed=0):
    """The `SHARE` block plus the one line the round prints.

    One line per detected row, because a bare ratio is exactly what a hardcoded number
    also produces -- the rows are the evidence that the counts were computed.

    `sent_known=False` marks the header when the caller could not say what actually
    left, so a reader knows the numerator rests on drafts rather than on sends.

    `unparsed` is the count of digest rows whose class token was not recognised. It is
    printed rather than swallowed: a denominator that quietly omits rows looks exactly
    like a smaller fleet.
    """
    lines = ["SHARE — detected problems ending in a verified action"]
    if not sent_known:
        lines.append(
            "  ⚠️ send state unknown — an action counts as taken when it was DRAFTED, so "
            "this numerator can credit a message that never left"
        )
    if unparsed:
        lines.append(
            "  ⚠️ %d CLASSIFICATION line(s) could not be read as a row — no class token in "
            "`DIGEST_CLASSES` at the class column — and are NOT in the denominator below. "
            "Add the class to `DIGEST_CLASSES` in drive-share.py if it is a real one."
            % unparsed
        )
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
    parsed = digest_rows(args.digest)
    if parsed is None:
        print(
            "drive-share: digest unreadable at %s -- refusing to report a share over a "
            "classification that could not be read" % args.digest,
            file=sys.stderr,
        )
        return REFUSED
    rows, unparsed = parsed
    if not rows and not unparsed and not classification_present(args.digest):
        print(
            "drive-share: digest at %s carries no CLASSIFICATION section -- refusing to "
            "report a share over a classification that could not be found" % args.digest,
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
    sent = None
    if args.sent:
        try:
            with open(args.sent, encoding="utf-8") as handle:
                sent = {
                    line.strip()[:8]
                    for line in handle
                    if line.strip() and not line.strip().startswith("#")
                }
        except OSError as exc:
            print("drive-share: sent list unreadable: %s" % exc, file=sys.stderr)
            return REFUSED
    print(
        render(
            measure(rows, entries, start, sent=sent),
            sent_known=sent is not None,
            unparsed=unparsed,
        )
    )
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
    share.add_argument(
        "--sent",
        default=None,
        help="file of the session ids this round actually sent to or posted a card for, one "
        "per line; without it a DRAFTED action counts as taken and the block says so",
    )
    args = parser.parse_args(argv)
    if args.command == "share":
        return cmd_share(args)
    parser.print_help(sys.stderr)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main())
