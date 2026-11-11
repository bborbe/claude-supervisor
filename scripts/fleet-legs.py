#!/usr/bin/env python3
"""The acting legs' counts, written into the round's digest — one home for their shape.

Rows 5b (keep alive) and 5c (flagged open) restart a crashed manager and open a page the
operator flagged, and both print a count. That count reached the round's **tool output** and
nothing else: `agents/fleet-sweep-reader.md` writes the digest at router row 2, before either
leg runs, and `commands/steps/output-shape.md` had no slot for them. So the round restarted a
manager, and the artifact that survives the round said nothing about it.

Measured 2026-11-06: `~/.claude/state/fleet-handback/2026-11-06T19-10Z.digest` is a real
69-line rendered round over 24 sessions, and `grep -c 'unactioned:'` over it returns **0** —
while `keepalive.md` and `flagged-open.md` both instruct the round to print those lines.

The fix is a persisted line, not a louder chat line: the digest carries the shape from the
moment the sweep writes it, and each leg replaces its own line once it has a reading.

Verbs, all against one digest path:

  --leg <key|leg> --count <n> [--floor|--exact]
        Replace that key's line with `<leg>: unactioned: <n> <key>`. `--floor` appends
        ` (lower bound, scan incomplete)`; a `dead-workers` count MUST carry one of the two,
        because a bare number reads as a measurement while the scan cannot finish inside its
        own timeout. Both are REFUSED for any other key — a floor marker on a count that is
        not a floor would teach the operator to ignore it.

  --leg <key|leg> --not-run <reason>
        Replace the leg's lines with one `not run — <reason> · <keys>` line. This is what a
        round whose 5b/5c did not execute renders, and it is the whole point of the file: a
        suppressed leg and a leg that ran and found nothing must never print the same thing.

  --check
        Exit 1 unless every one of the four keys is covered exactly once, by a count line or
        by a not-run line that names it. Exit 2 when the digest cannot be read or written.

⚠️ **The section is written by the sweep reader, not created here.** A digest that carries no
`LEGS` section is the pre-fix shape and `--check` fails on it — that is the case the test
pins, and it is why `--count` creating the section is a convenience for a hand-run, never the
round's path: a round that creates its own section has already lost the not-run reading.

⚠️ **The four keys and their legs live here, once.** `commands/steps/output-shape.md` item 14
renders the lines and `agents/fleet-sweep-reader.md` seeds them; neither restates the mapping.

Exit codes: 0 ok, 1 the digest is missing a line (`--check`), 2 the digest could not be read
or written. Never 0 on a failed write — a count that silently did not land is the defect.
"""
import argparse
import os
import re
import sys
import tempfile

#: Render order. A list, not a set: the digest's line order is part of its shape.
KEYS = ["lapsed-managers", "topicless-goal-managers", "dead-workers", "operator-flagged"]
LEG_OF = {
    "lapsed-managers": "keep-alive",
    "topicless-goal-managers": "keep-alive",
    "dead-workers": "keep-alive",
    "operator-flagged": "flagged-open",
}
KEYS_OF = {
    "keep-alive": ["lapsed-managers", "topicless-goal-managers", "dead-workers"],
    "flagged-open": ["operator-flagged"],
}
FLOOR_KEY = "dead-workers"
FLOOR_SUFFIX = " (lower bound, scan incomplete)"
#: The flagged-open count. Its number means two different things — rows left by a batch that
#: ran, and rows waiting because it never did — so the line carries which one.
BATCH_KEY = "operator-flagged"
SECTION = "LEGS"
NOT_RUN = "not run — "
#: The sweep reader's seed reason. `--check` treats it as NOT RECORDED: the shape exists,
#: but no leg has spoken for that key, so a round that reached the render with this text
#: still standing is a round whose leg neither counted nor said why — the failure the check
#: exists for. A leg that genuinely did not run writes its OWN reason, which reads as recorded.
SEED_REASON = "no reading recorded this round"

COUNT_RE = re.compile(
    r"^(?P<leg>[\w-]+): unactioned: (?P<n>\d+) (?P<key>[\w-]+)"
    r"(?P<floor> \(lower bound, scan incomplete\))?"
    r"(?P<batch> \((?:batch invoked|batch not invoked)\))?$"
)
NOTRUN_RE = re.compile(r"^(?P<leg>[\w-]+): not run — (?P<reason>.+?)(?: · (?P<keys>.+))?$")

EXIT_INCOMPLETE = 1
EXIT_FAILED = 2


def resolve(target):
    """-> (leg, [keys]) for a key or a leg name; None when it is neither."""
    if target in LEG_OF:
        return LEG_OF[target], [target]
    if target in KEYS_OF:
        return target, list(KEYS_OF[target])
    return None


def split_section(lines):
    """-> (before, body, after) around the `LEGS` header, or (lines, None, []) when absent.

    The body is every following line that starts with whitespace; the section ends at the
    first line that does not (a following section header, or a blank line then one).
    """
    for i, line in enumerate(lines):
        if line.strip() == SECTION:
            j = i + 1
            while j < len(lines) and lines[j].startswith((" ", "\t")):
                j += 1
            return lines[: i + 1], lines[i + 1 : j], lines[j:]
    return list(lines), None, []


def parse(body):
    """-> (entries, errors). An entry is (leg, kind, payload, keys).

    `kind` is `count` (payload = (n, floor, batch)) or `notrun` (payload = reason). `batch`
    is the ` (batch invoked)` / ` (batch not invoked)` marker, or `` for a key that has no
    batch to report. A line that matches neither is an error, never silently dropped: an
    unparseable line is a count the round wrote and nobody can read.
    """
    entries, errors = [], []
    for line in body:
        text = line.strip()
        if not text:
            continue
        m = COUNT_RE.match(text)
        if m:
            entries.append(
                (
                    m.group("leg"),
                    "count",
                    (int(m.group("n")), bool(m.group("floor")), m.group("batch") or ""),
                    [m.group("key")],
                )
            )
            continue
        m = NOTRUN_RE.match(text)
        if m:
            # A not-run line may omit its key list when it covers the WHOLE leg — the sweep
            # seeds it that way, and the leg name then implies the keys. Once one key of the
            # leg has its own count the remainder is a strict subset and the list is printed,
            # so a partial not-run reading is never mistaken for the whole leg.
            keys = [k.strip() for k in (m.group("keys") or "").split(",") if k.strip()]
            if not keys:
                keys = list(KEYS_OF.get(m.group("leg"), []))
            entries.append((m.group("leg"), "notrun", m.group("reason"), keys))
            continue
        errors.append(text)
    return entries, errors


def order_key(entry):
    keys = entry[3]
    return min((KEYS.index(k) for k in keys if k in KEYS), default=len(KEYS))


def render(entries):
    out = []
    for leg, kind, payload, keys in sorted(entries, key=order_key):
        if kind == "count":
            n, floor, batch = payload
            suffix = FLOOR_SUFFIX if floor else ""
            out.append("  %s: unactioned: %d %s%s%s" % (leg, n, keys[0], suffix, batch))
        else:
            # Print the key list only when it is a STRICT SUBSET of the leg's keys: a line
            # covering the whole leg is unambiguous from the leg name alone, and a partial
            # one must name what it still covers.
            partial = sorted(keys) != sorted(KEYS_OF.get(leg, keys))
            suffix = " · %s" % ", ".join(keys) if partial else ""
            out.append("  %s: %s%s%s" % (leg, NOT_RUN, payload, suffix))
    return out


def set_count(entries, key, n, floor, batch=""):
    """Replace `key`'s reading with a count line, dropping it from any not-run line."""
    entries = [
        (leg, kind, payload, [k for k in keys if k != key])
        for leg, kind, payload, keys in entries
    ]
    entries = [e for e in entries if e[3]]
    entries.append((LEG_OF[key], "count", (n, floor, batch), [key]))
    return entries


def set_not_run(entries, leg, keys, reason):
    """Collapse the leg's named keys into one not-run line."""
    entries = [
        (l, kind, payload, [k for k in ks if k not in keys])
        for l, kind, payload, ks in entries
    ]
    entries = [e for e in entries if e[3]]
    entries.append((leg, "notrun", reason, list(keys)))
    return entries


def check(entries, errors):
    """-> [problems]. Empty means every key carries a RECORDED reading.

    A key covered by a count is recorded. A key covered by a `not run` line is recorded
    only when the reason is not the sweep's seed — the seed says "nobody has spoken for
    this key", which is the state a render must not mistake for a leg that ran and found
    nothing.
    """
    problems = ["unparseable LEGS line: %r" % e for e in errors]
    seen = {}
    for leg, kind, payload, keys in entries:
        for key in keys:
            if key not in LEG_OF:
                problems.append("unknown key %r in a LEGS line" % key)
                continue
            seen.setdefault(key, []).append((kind, payload))
            if LEG_OF[key] != leg:
                problems.append(
                    "key %r is rendered under leg %r but belongs to %r"
                    % (key, leg, LEG_OF[key])
                )
    for key in KEYS:
        readings = seen.get(key, [])
        if not readings:
            problems.append("no line for %r" % key)
        elif len(readings) > 1:
            problems.append("%r is covered %d times" % (key, len(readings)))
        elif readings[0][0] == "notrun" and readings[0][1] == SEED_REASON:
            problems.append(
                "no reading recorded for %r — the sweep's seed is still standing, so the "
                "leg neither counted nor said why" % key
            )
    return problems


def write_atomic(path, lines, keep=None):
    """Replace `path` atomically, preserving the mode and mtime of the file it replaces.

    ⚠️ **The mtime is not cosmetic.** `scripts/fleet-digest.py` ages a round by this file's
    mtime whenever its `snapshot written:` line names no parseable baseline (`join-degraded`
    and the other `skipped` forms), so a leg rewrite that bumped the mtime would make a
    lapsed round read fresh to the staleness watcher — a leg write must not be able to
    silence the check that reports a dead loop.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".fleet-legs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        # ⚠️ Mode and mtime are set on the TEMP file, before the replace. Doing it after
        # would mean a failed chmod/utime reports failure for a write that already landed —
        # the render would say the count did not persist when it did.
        if keep is not None:
            os.chmod(tmp, keep.st_mode & 0o777)
            os.utime(tmp, (keep.st_atime, keep.st_mtime))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Write the acting legs' counts into the round's digest."
    )
    parser.add_argument("--digest", required=True, help="this round's .digest path")
    parser.add_argument(
        "--check", action="store_true", help="verify the four lines are present"
    )
    parser.add_argument("--leg", help="a count key, or a leg name for --not-run")
    parser.add_argument("--count", type=int, help="the leg's reading for this key")
    parser.add_argument(
        "--floor", action="store_true", help="label dead-workers as a lower bound"
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help="assert the dead-worker scan completed, so the count is not a floor",
    )
    parser.add_argument("--not-run", dest="not_run", help="why the leg did not execute")
    parser.add_argument(
        "--invoked",
        action="store_true",
        help="the flagged-open leg ran its batch; the count is what the batch left",
    )
    parser.add_argument(
        "--not-invoked",
        dest="not_invoked",
        action="store_true",
        help="the flagged-open leg only counted the debt; the batch never ran",
    )
    args = parser.parse_args(argv)

    if not args.check and args.count is None and args.not_run is None:
        parser.error("one of --check, --count or --not-run is required")
    if args.count is not None and args.not_run is not None:
        parser.error("--count and --not-run are mutually exclusive")
    if args.invoked and args.not_invoked:
        parser.error("--invoked and --not-invoked are mutually exclusive")
    if args.floor and args.exact:
        parser.error("--floor and --exact are mutually exclusive")
    if args.check and (args.leg or args.count is not None or args.not_run is not None):
        parser.error("--check takes no --leg, --count or --not-run")
    if args.count is not None and args.count < 0:
        parser.error("--count cannot be negative — a negative reading is not a count")
    if args.not_run is not None:
        reason = args.not_run.strip()
        if not reason:
            parser.error("--not-run needs a reason — an empty one reads as no line at all")
        if " · " in reason:
            parser.error(
                "--not-run cannot contain ' · ' — that separator introduces the key list, "
                "so the reason would be read as keys"
            )

    try:
        with open(args.digest, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print("fleet-legs: cannot read %s: %s" % (args.digest, exc), file=sys.stderr)
        return EXIT_FAILED

    lines = text.splitlines()
    before, body, after = split_section(lines)

    if args.check:
        if body is None:
            print(
                "fleet-legs: %s carries no %s section — the round rendered without the "
                "acting legs' counts" % (args.digest, SECTION),
                file=sys.stderr,
            )
            return EXIT_INCOMPLETE
        entries, errors = parse(body)
        problems = check(entries, errors)
        if problems:
            for p in problems:
                print("fleet-legs: %s" % p, file=sys.stderr)
            return EXIT_INCOMPLETE
        print("legs ok: %s carries a recorded reading for all %d keys" % (args.digest, len(KEYS)))
        return 0

    resolved = resolve(args.leg or "")
    if resolved is None:
        print(
            "fleet-legs: --leg must be one of %s"
            % ", ".join(KEYS + list(KEYS_OF)),
            file=sys.stderr,
        )
        return EXIT_FAILED
    leg, keys = resolved

    entries, errors = parse(body or [])
    if errors:
        for e in errors:
            print("fleet-legs: unparseable LEGS line: %r" % e, file=sys.stderr)
        return EXIT_FAILED

    if args.count is not None:
        if len(keys) != 1:
            print(
                "fleet-legs: --count needs one key, not a leg — %r covers %s"
                % (args.leg, ", ".join(keys)),
                file=sys.stderr,
            )
            return EXIT_FAILED
        key = keys[0]
        if key == FLOOR_KEY:
            # A bare dead-worker number reads as a measurement, and the scan cannot finish
            # inside its own timeout. The author must say which one it is.
            if not (args.floor or args.exact):
                print(
                    "fleet-legs: a %r count needs --floor (the scan did not finish) or "
                    "--exact (it did) — a bare number reads as a measurement"
                    % key,
                    file=sys.stderr,
                )
                return EXIT_FAILED
        elif args.floor or args.exact:
            print(
                "fleet-legs: --floor / --exact describe the %r scan only, not %r"
                % (FLOOR_KEY, key),
                file=sys.stderr,
            )
            return EXIT_FAILED
        if key == BATCH_KEY:
            # One line carried two meanings: "the leg ran and left N" and "the leg never ran,
            # N rows are waiting". A reader — human or model — cannot tell them apart, and the
            # second is the failure this leg exists to remove.
            if not (args.invoked or args.not_invoked):
                print(
                    "fleet-legs: a %r count must say whether the batch was invoked — "
                    "--invoked (the leg ran it) or --not-invoked (it only counted the debt)"
                    % key,
                    file=sys.stderr,
                )
                return EXIT_FAILED
        elif args.invoked or args.not_invoked:
            print(
                "fleet-legs: --invoked / --not-invoked describe the %r batch only, not %r"
                % (BATCH_KEY, key),
                file=sys.stderr,
            )
            return EXIT_FAILED
        batch = " (batch invoked)" if args.invoked else (" (batch not invoked)" if args.not_invoked else "")
        new_body = render(set_count(entries, key, args.count, args.floor, batch))
    else:
        new_body = render(set_not_run(entries, leg, keys, args.not_run))

    if body is None:
        # A digest that carries no LEGS section is the pre-fix shape. Insert the header AND
        # its body together, in the template position — immediately before NOTES — rather
        # than putting the header there and appending the body at EOF, which leaves a
        # section whose lines are outside it and fails the very next `--check`.
        cut = next(
            (i for i, line in enumerate(before) if line.strip().startswith("NOTES")),
            len(before),
        )
        new_lines = before[:cut] + [SECTION] + new_body + before[cut:]
    else:
        new_lines = before + new_body + after
    try:
        write_atomic(args.digest, new_lines, keep=os.stat(args.digest))
    except OSError as exc:
        print("fleet-legs: cannot write %s: %s" % (args.digest, exc), file=sys.stderr)
        return EXIT_FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main())
