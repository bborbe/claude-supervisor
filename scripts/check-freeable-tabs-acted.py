#!/usr/bin/env python3
"""A drive round must close the `FREEABLE TABS` rows it lists, not merely reap them.

Measured 2026-11-12 on round `2026-11-12T10-08-16Z`: the payload listed

    Update All Service Repos Go Modules 2026-11 · Update All Service Repos' Go Modules … · finished · http://…jump?pane=46&t=…

under `FREEABLE TABS`, and the round's own summary queued it as a **reap** —

    Queued but not yet sent by me: 2 reaps (…, Update All Service Repos Go Modules 2026-11 …)

— while `pane 46` appears **zero** times in that session's transcript. The close was never
attempted, so there is no refusal token either; absence of a refusal is not evidence of an
attempted close. The row was verified `completed` / `done` / 0 open boxes before being
queued, which makes this a routing failure rather than a careless one.

`commands/steps/drive.md` says a `finished` row appears in **both** populations and that the
`REAPS` loop cannot reach `FREEABLE TABS`. The instruction was present and correct; the
caller sent the reap and stopped. Prose a caller can skip is the defect, so the pass is now
a named step with its own required line and this check is its guard.

    freeable-tabs: rows <N> · acted <M> · missing <N-M>

WHY IT IS A SCRIPT. The denominator is a parse over one payload block with a link test; the
numerator is a read of the round's action record; the verdict is an exit code. A sentence
claiming the pass ran would satisfy every shape-only check of the same requirement.

**`missing` is scoped to rows carrying a resolved link.** A row that renders
`no pane — <reason>` is a headless row or an unresolvable one: the headless branch closes it
through the same script but the row is not the population this guard exists for, and counting
it would report a deficit on every quiet round. `rows` counts the whole block so the line
still proves the block was read.

**This check reports; it never fails the round.** A round that refuses to complete over one
unclosed row converts a missed close into a missed round, and the fleet already loses rounds
to that shape. Exit 1 is a verdict for the caller to *print*, not a reason to abandon the leg.

Two halves, both required:

  **Static (always, no arguments).** The contract lives in two files and a later edit to
  either can drop it silently:
    (a) `commands/steps/drive.md` names this check and the line it must print.
    (b) `commands/steps/output-shape.md` mandates the line, so the round cannot omit it.

  **Artifacts (`--drive`, optional `--sent`).** Given a round's payload, count the freeable
  rows, read the action record, and report a deficit.

Exit codes: 0 clean, 1 deficit, 2 refused (an unreadable payload or a malformed invocation
is not a clean round, and must never render as one).
"""
import re
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROUND = REPO / "commands" / "steps" / "drive.md"
OUTPUT_SHAPE = REPO / "commands" / "steps" / "output-shape.md"

#: Exit code for an input this gate will not measure. Distinct from 1 (a deficit).
REFUSED = 2

#: This check's own name, which the round step must carry.
CHECK_NAME = "check-freeable-tabs-acted.py"

#: The line the round step and the output shape must both carry. Anchored on all four counts
#: together, because `freeable-tabs:` alone is satisfied by any sentence about freeable tabs.
ROUND_LINE_MARKER = "freeable-tabs: rows <N> · closed <M> · refused <K> · skipped <J>"
OUTPUT_SHAPE_MARKER = "freeable-tabs: rows <N> · acted <M> · missing <N-M>"

#: Column-0 section heads. A head ends whatever block was open, so the parse cannot run past
#: the block it is counting -- the payload carries `ESCALATION`, `CLOSERS` and `RETAINED`
#: after this one, each with rows that would otherwise be counted as freeable.
SECTION_HEADS = (
    "UNVERIFIABLE ESCALATIONS",
    "UNVERIFIABLE NUDGES",
    "UNVERIFIABLE HELD BACK",
    "UNVERIFIABLE",
    "FREEABLE TABS",
    "Waiting on your keystroke",
    "STALE WAITS",
    "ESCALATION",
    "RETAINED",
    "CLOSED",
    "PARKED",
    "REAPS",
    "NUDGES",
    "LEDGER",
)

#: A freeable row: `<name> · <class> · <link or reason>`. The name is everything before the
#: first ` · `; the class and the third field are not needed to count, only the link is.
FREEABLE_ROW = re.compile(r"^\s*(?P<name>[^·]+?)\s*·")

#: A resolved pane link. The absence of this is what `no pane — <reason>` renders.
RESOLVED_LINK = "http://"


def _norm_name(name):
    """Casefolded, decoration-stripped label, for the row/record join.

    Mirrors `check-drive-nudges-sent.py`'s `_norm_recipient` and `fleet-board.py`'s
    `_norm_session_name`: a registry label carries a `⚙ ` marker the action record may not,
    so an exact-string join reports a correctly-closed row as missing. Copied rather than
    imported because these scripts ship standalone.
    """
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = re.sub(r"^[^\w(]+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def freeable_rows(text):
    """(name, has_resolved_link) per row under `FREEABLE TABS`, in payload order."""
    rows = []
    block = None
    for line in text.splitlines():
        stripped = line.strip()
        head = next(
            (
                name
                for name in SECTION_HEADS
                if stripped.startswith(name) and not line[:1].isspace()
            ),
            None,
        )
        if head:
            block = head
            continue
        if block != "FREEABLE TABS":
            continue
        match = FREEABLE_ROW.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        if name:
            rows.append((name, RESOLVED_LINK in line))
    return rows


def closed_names(path):
    """Normalised names the round closed, or None when the record exists but is unreadable.

    A missing file is **nothing recorded, not an unknown** -- the record is per-round by
    design, so its absence is a statement about this round. `None` is reserved for a file
    that exists and cannot be read, which is a refusal rather than a clean zero.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    out = set()
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        action, tab, recipient = entry.partition("\t")
        if tab and action.strip() == "closed":
            out.add(_norm_name(recipient.strip()))
    return out


def sent_path_for(drive_path):
    """`<round>.sent` beside the payload: `.../2026-11-12T10-08-16Z.drive` -> `….sent`."""
    path = Path(drive_path)
    if path.suffix == ".drive":
        return path.with_suffix(".sent")
    return Path(str(path) + ".sent")


def static_failures():
    failures = []
    if not ROUND.exists():
        return ["%s does not exist -- the round step has no body to check" % ROUND]
    if not OUTPUT_SHAPE.exists():
        return [
            "%s does not exist -- the never-omitted rule has no body to check"
            % OUTPUT_SHAPE
        ]

    round_step = ROUND.read_text(encoding="utf-8")
    output_shape = OUTPUT_SHAPE.read_text(encoding="utf-8")

    if ROUND_LINE_MARKER not in round_step:
        failures.append(
            "%s no longer requires `%s` -- the step would print nothing and a round that "
            "skipped it would look complete" % (ROUND, ROUND_LINE_MARKER)
        )
    if CHECK_NAME not in round_step:
        failures.append(
            "%s no longer runs `%s` -- the 2026-11-12 round queued a reap for a freeable row "
            "and nothing would have reported it" % (ROUND, CHECK_NAME)
        )
    if OUTPUT_SHAPE_MARKER not in output_shape:
        failures.append(
            "%s no longer mandates `%s` -- the round may compute the counts and render "
            "silence" % (OUTPUT_SHAPE, OUTPUT_SHAPE_MARKER)
        )
    return failures


def artifact_failures(drive_path, sent_path):
    """(failures, line, refused) for one round's payload plus its action record.

    `refused` is True when the round cannot be MEASURED. It is deliberately distinct from a
    deficit: a refusal says nothing about whether the rows were closed, so collapsing it into
    exit 1 made a broken read render as a missed close, which is the one conclusion this
    guard must never reach by accident.
    """
    try:
        text = Path(drive_path).read_text(encoding="utf-8")
    except OSError as exc:
        return ["payload unreadable: %s" % exc], "", True

    rows = freeable_rows(text)
    closed = closed_names(sent_path)
    if closed is None:
        return ["action record unreadable: %s" % sent_path], "", True

    actionable = [name for name, linked in rows if linked]
    missing = [name for name in actionable if _norm_name(name) not in closed]
    line = "freeable-tabs: rows %d · acted %d · missing %d" % (
        len(rows),
        len(actionable) - len(missing),
        len(missing),
    )
    if missing:
        return (
            [
                "%d freeable row(s) carried a resolved link and no `closed` entry: %s -- the "
                "round listed them and did not close them, which is the 2026-11-12 defect"
                % (len(missing), "; ".join(missing[:3]))
            ],
            line,
            False,
        )
    return [], line, False


def _parse(argv):
    """(drive, sent, error). Accepts `--flag <path>` and `--flag=<path>`.

    An unknown flag or a positional argument is an error rather than an absence: skipping it
    silently would fall through to the static-only run and exit 0 over an unchecked round. No
    arguments at all is the Makefile/CI static run and is legal.
    """
    values = {"--drive": None, "--sent": None}
    i = 0
    while i < len(argv):
        token = argv[i]
        name, eq, inline = token.partition("=")
        if name not in values:
            if token.startswith("-"):
                return None, None, "unknown flag %s" % name
            return None, None, (
                "unexpected argument %r -- artifact paths must be given as --drive/--sent"
                % token
            )
        if eq:
            value = inline
            i += 1
        elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            value = argv[i + 1]
            i += 2
        else:
            return None, None, "%s given with no value" % name
        if not value:
            return None, None, "%s given with an empty value" % name
        values[name] = value
    return values["--drive"], values["--sent"], None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    drive_path, sent_path, parse_error = _parse(argv)

    failures = static_failures()
    if parse_error:
        print("check-freeable-tabs-acted: refused -- %s" % parse_error, file=sys.stderr)
        return REFUSED
    if sent_path and not drive_path:
        # `--sent` alone would fall through to the static-only run and exit 0 with the round
        # unmeasured -- the same green-over-nothing the valueless-flag guard closes.
        print(
            "check-freeable-tabs-acted: refused -- --sent requires --drive: an action record "
            "cannot be measured against a payload it was not given",
            file=sys.stderr,
        )
        return REFUSED

    line = ""
    refused = False
    if drive_path:
        artifact_failure, line, refused = artifact_failures(
            drive_path, sent_path or sent_path_for(drive_path)
        )
        failures.extend(artifact_failure)
        if line:
            print(line)
        elif refused:
            print("freeable-tabs: refused -- see the failures below")

    if refused:
        print("check-freeable-tabs-acted: REFUSED", file=sys.stderr)
        for failure in failures:
            print("  - %s" % failure, file=sys.stderr)
        return REFUSED

    if failures:
        print("check-freeable-tabs-acted: FAIL", file=sys.stderr)
        for failure in failures:
            print("  - %s" % failure, file=sys.stderr)
        return 1

    scope = "static contract"
    if drive_path:
        scope += " + payload %s" % drive_path
    print(
        "freeable-tabs-acted ok: the round step names this check and its line, output-shape "
        "mandates the line, and no freeable row with a resolved link is unclosed (%s)" % scope
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
