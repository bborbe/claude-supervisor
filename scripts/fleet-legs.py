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

  --per-kind "tasks=<o>/<u>/<e> goals=… topics=…" --invoked|--not-invoked
        Replace the flagged-open reading with
        `flagged-open: tasks=<o>/<u>/<e> goals=<o>/<u>/<e> topics=<o>/<u>/<e> <marker>`.
        One `unactioned: <n>` cannot say WHICH kind the operator's flag reached — a round
        that opened three topics and a round that opened nothing printed the same line — so
        each kind carries `opened/declined/eligible` and `opened + declined == eligible` is
        refused at the writer AND the reader when it does not hold. It supersedes the single
        count rather than joining it: both cover `operator-flagged`, and two lines for one
        key is the duplicate `--check` refuses.

  --check
        Exit 1 unless every one of the four keys is covered exactly once, by a count line, a
        per-kind line or a not-run line that names it. Exit 2 when the digest cannot be read
        or written.

  --status
        Print the round's ONE-LINE verdict — `legs: OK …`, `legs: INCOMPLETE — <reason>` or
        `legs: UNKNOWN — <reason>` — and exit 0 / 1 / 2. The render copies this string
        instead of being told to remember a sentence: a verdict a model paraphrases is a
        verdict that can be softened, which is how a failed round reads as a clean one.

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

#: The per-kind reading — the shape that answers *which* of the three kinds the flag reached.
#: A single `unactioned: <n>` cannot: a round that opened three topics and a round that opened
#: nothing print the same line. Each kind carries `opened/declined/eligible`, so
#: `opened + declined == eligible` is checkable per kind instead of asserted once for all three.
PER_KIND_LEG = "flagged-open"
PER_KIND_KEYS = ("tasks", "goals", "topics")
PER_KIND_RE = re.compile(
    r"^(?P<leg>[\w-]+): tasks=(?P<tasks>\d+/\d+/\d+) goals=(?P<goals>\d+/\d+/\d+)"
    r" topics=(?P<topics>\d+/\d+/\d+)"
    r"(?P<batch> \((?:batch invoked|batch not invoked)\))?$"
)

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


def per_kind_adds_up(triple):
    """Is `<opened>/<declined>/<eligible>` arithmetically a reading?

    `opened + declined == eligible` is the whole point of carrying three numbers instead of
    one: a count that does not add up is a count nobody can reconcile against the selector,
    so it is refused at the writer AND at the reader rather than printed and trusted.
    """
    try:
        opened, declined, eligible = (int(p) for p in triple.split("/"))
    except ValueError:
        return False
    return min(opened, declined, eligible) >= 0 and opened + declined == eligible


def parse_per_kind(spec):
    """-> tuple of three `o/u/e` triples, or None when `spec` is not a per-kind reading.

    The argument is order-bearing on purpose: `tasks=… goals=… topics=…` in that order, each
    appearing exactly once. A caller that transposes two kinds would otherwise write a line
    whose labels are a lie, and nothing downstream could tell.
    """
    if spec is None:
        return None
    parts = spec.split()
    if len(parts) != len(PER_KIND_KEYS):
        return None
    out = []
    for part, kind in zip(parts, PER_KIND_KEYS):
        head, sep, triple = part.partition("=")
        if head != kind or not sep or not per_kind_adds_up(triple):
            return None
        out.append(triple)
    return tuple(out)


def parse(body):
    """-> (entries, errors). An entry is (leg, kind, payload, keys).

    `kind` is `count` (payload = (n, floor, batch)), `perkind` (payload = (triples, batch))
    or `notrun` (payload = reason). `batch` is the ` (batch invoked)` / ` (batch not invoked)`
    marker, or `` for a key that has no batch to report. A line that matches none of the three
    is an error, never silently dropped: an unparseable line is a count the round wrote and
    nobody can read.
    """
    entries, errors = [], []
    for line in body:
        text = line.strip()
        if not text:
            continue
        m = PER_KIND_RE.match(text)
        if m:
            batch = m.group("batch") or ""
            # The per-kind shape belongs to the leg whose single count it replaces, and it
            # carries the same batch marker that count is required to carry — a per-kind line
            # on another leg, or without the marker, is a line nobody wrote on purpose.
            if m.group("leg") != PER_KIND_LEG or not batch:
                errors.append(text)
                continue
            triples = tuple(m.group(k) for k in PER_KIND_KEYS)
            if any(not per_kind_adds_up(t) for t in triples):
                errors.append(text)
                continue
            entries.append(
                (m.group("leg"), "perkind", (triples, batch), [BATCH_KEY])
            )
            continue
        m = COUNT_RE.match(text)
        if m:
            key, batch = m.group("key"), m.group("batch") or ""
            # The same invariant the writer enforces, read back: a bare operator-flagged count
            # is ambiguous, and a marker on a key with no batch is a line nobody wrote on purpose.
            # A digest carrying either is not a complete reading, which is what --check reports.
            if key == BATCH_KEY and not batch:
                errors.append(text)
                continue
            if key != BATCH_KEY and batch:
                errors.append(text)
                continue
            entries.append(
                (
                    m.group("leg"),
                    "count",
                    (int(m.group("n")), bool(m.group("floor")), batch),
                    [key],
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
        elif kind == "perkind":
            triples, batch = payload
            readings = " ".join(
                "%s=%s" % (k, t) for k, t in zip(PER_KIND_KEYS, triples)
            )
            out.append("  %s: %s%s" % (leg, readings, batch))
        else:
            # Print the key list only when it is a STRICT SUBSET of the leg's keys: a line
            # covering the whole leg is unambiguous from the leg name alone, and a partial
            # one must name what it still covers.
            partial = sorted(keys) != sorted(KEYS_OF.get(leg, keys))
            suffix = " · %s" % ", ".join(keys) if partial else ""
            out.append("  %s: %s%s%s" % (leg, NOT_RUN, payload, suffix))
    return out


def set_per_kind(entries, triples, batch):
    """Replace the flagged-open key's reading with the per-kind line.

    It supersedes the single count rather than joining it: both cover `operator-flagged`, and
    two lines for one key is the duplicate `--check` refuses. A round that can say `tasks=…`
    has no use for the total it decomposes into.
    """
    entries = [
        (leg, kind, payload, [k for k in keys if k != BATCH_KEY])
        for leg, kind, payload, keys in entries
    ]
    entries = [e for e in entries if e[3]]
    entries.append((PER_KIND_LEG, "perkind", (triples, batch), [BATCH_KEY]))
    return entries


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


def status(digest):
    """-> (line, exit code). The round's own verdict, as ONE string the render copies.

    `--check` already answers the question, but its answer arrives as a set of stderr lines
    plus an exit code, and the round has to turn that into something the operator reads. A
    render told to "render it as INCOMPLETE" is a sentence a model is asked to remember; this
    returns the exact line instead, so the failure cannot be softened in transcription.

    Exit 1 is INCOMPLETE (the section is absent or not a complete reading) and exit 2 is
    UNKNOWN (the digest could not be read) — the same split `--check` makes, for the same
    reason: a failed read and a round whose legs were suppressed must not print alike.
    """
    try:
        with open(digest, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return "legs: UNKNOWN — cannot read %s: %s" % (digest, exc), EXIT_FAILED
    before, body, after = split_section(text.splitlines())
    if body is None:
        return (
            "legs: INCOMPLETE — the digest carries no %s section, so no leg spoke"
            % SECTION,
            EXIT_INCOMPLETE,
        )
    entries, errors = parse(body)
    problems = check(entries, errors)
    if problems:
        return "legs: INCOMPLETE — %s" % problems[0], EXIT_INCOMPLETE
    return "legs: OK — every acting leg recorded a reading", 0


def write_digest(digest, before, body, after, new_body):
    """Splice `new_body` into the digest and persist it -> exit code.

    One home for the splice: a digest that carries no LEGS section is the pre-fix shape, and
    the header must go in WITH its body, in the template position — immediately before NOTES
    — rather than the header there and the body at EOF, which leaves a section whose lines
    fall outside it and fails the very next `--check`.
    """
    if body is None:
        cut = next(
            (i for i, line in enumerate(before) if line.strip().startswith("NOTES")),
            len(before),
        )
        new_lines = before[:cut] + [SECTION] + new_body + before[cut:]
    else:
        new_lines = before + new_body + after
    try:
        write_atomic(digest, new_lines, keep=os.stat(digest))
    except OSError as exc:
        print("fleet-legs: cannot write %s: %s" % (digest, exc), file=sys.stderr)
        return EXIT_FAILED
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Write the acting legs' counts into the round's digest."
    )
    parser.add_argument("--digest", required=True, help="this round's .digest path")
    parser.add_argument(
        "--check", action="store_true", help="verify the four lines are present"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print the round's one-line legs verdict and exit 0 / 1 / 2",
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
        "--per-kind",
        dest="per_kind",
        help=(
            "the flagged-open leg's reading, per kind: "
            '"tasks=<opened>/<declined>/<eligible> goals=… topics=…"'
        ),
    )
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

    if (
        not args.check
        and not args.status
        and args.count is None
        and args.not_run is None
        and args.per_kind is None
    ):
        parser.error(
            "one of --check, --status, --count, --per-kind or --not-run is required"
        )
    if args.status and (
        args.check
        or args.leg
        or args.count is not None
        or args.not_run is not None
        or args.per_kind is not None
    ):
        parser.error(
            "--status takes no --check, --leg, --count, --per-kind or --not-run"
        )
    if args.count is not None and args.not_run is not None:
        parser.error("--count and --not-run are mutually exclusive")
    if args.per_kind is not None and (
        args.count is not None or args.not_run is not None or args.leg
    ):
        parser.error(
            "--per-kind is the whole flagged-open reading — it takes no --leg, --count "
            "or --not-run"
        )
    if args.invoked and args.not_invoked:
        parser.error("--invoked and --not-invoked are mutually exclusive")
    if args.floor and args.exact:
        parser.error("--floor and --exact are mutually exclusive")
    if args.check and (
        args.leg
        or args.count is not None
        or args.not_run is not None
        or args.per_kind is not None
    ):
        parser.error("--check takes no --leg, --count, --per-kind or --not-run")
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

    if args.status:
        # Deliberately after the read above: an unreadable digest must report UNKNOWN from
        # `status()` too, and reaching it twice is cheaper than two spellings of the verdict.
        line, code = status(args.digest)
        print(line)
        return code

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

    if args.per_kind is not None:
        # Resolved before `--leg`, which this verb does not take: the per-kind line belongs to
        # exactly one leg, so asking the caller to name it would be a second place to get it
        # wrong. These two refusals sit here, not in argparse, so they return EXIT_FAILED like
        # the sibling `--count` guards rather than SystemExit — one guard, one shape.
        if not (args.invoked or args.not_invoked):
            # Without the marker a reader cannot tell a leg that ran and found nothing from
            # one that never invoked the batch — the ambiguity the count shape already solved.
            print(
                "fleet-legs: --per-kind needs --invoked or --not-invoked — without the "
                "marker an unactioned count reads as a batch that ran and found nothing",
                file=sys.stderr,
            )
            return EXIT_FAILED
        triples = parse_per_kind(args.per_kind)
        if triples is None:
            print(
                "fleet-legs: --per-kind must read exactly "
                '"tasks=<opened>/<declined>/<eligible> goals=… topics=…", each kind once, '
                "in that order, with opened + declined == eligible",
                file=sys.stderr,
            )
            return EXIT_FAILED
        entries, errors = parse(body or [])
        if errors:
            for e in errors:
                print("fleet-legs: unparseable LEGS line: %r" % e, file=sys.stderr)
            return EXIT_FAILED
        batch = " (batch invoked)" if args.invoked else " (batch not invoked)"
        new_body = render(set_per_kind(entries, triples, batch))
        return write_digest(args.digest, before, body, after, new_body)

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

    return write_digest(args.digest, before, body, after, new_body)


if __name__ == "__main__":
    sys.exit(main())
