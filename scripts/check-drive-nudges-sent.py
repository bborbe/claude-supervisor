#!/usr/bin/env python3
"""A drive round must report how many nudges it drafted against how many it sent.

Measured 2026-11-11 on round `2026-11-11T08-51-29Z`: the payload drafted **5 distinct
nudge recipients** (five `TO:` lines under `NUDGES`, plus one under `UNVERIFIABLE NUDGES`
whose recipient the `NUDGES` block already named), and the round recorded **no send at
all**. It still read as complete, because `commands/steps/drive.md` requires `Sent: <n>`
and `Sent: 0` renders exactly like `Sent: 5` in a report nobody reconciles.

The miss is not cosmetic. `agents/fleet-drive.md` stamps the ledger at DRAFT time, so a
lost send is suppressed on the next round rather than retried -- and step 5b then closes a
nudged row that is still idle, i.e. it closes a session that never received the nudge.

Three counts, printed every round and never replaceable by `Sent:` alone:

    nudges: drafted <N> · sent <M> · missing <N-M>

WHY IT IS A SCRIPT. Every clause is a rule a reader can satisfy by writing a plausible
sentence. The denominator is a parse over three payload blocks with a dedupe rule (the
`UNVERIFIABLE NUDGES` block is a transcription instruction, so the same recipient can
appear twice); the numerator is a read of a per-round record; and the verdict is an exit
code. A hardcoded ratio would pass every shape-only check of the same requirement.

**What `M` is, and what it is not.** `M` is the round's own action record --
`~/.claude/state/fleet-handback/<round>.sent`, one entry per recipient, written by the send
step as it acts. It is deliberately **per-round**: a shared `/tmp/drive-sent.txt` plus a
truncate leaves a skipped send leg holding the PREVIOUS round's ids, which is the exact
failure this gate exists to catch. A round that skips the send step writes no file, so it
reports `missing <N>` rather than inheriting a clean bill.

**`missing` is "the round never touched this line", not "this line was not messaged".** A
drafted line has three legitimate outcomes and only silence is a defect: a `NUDGES` line is
messaged, a `REAPS` line whose target is a finished headless row is **closed** through
`close-worker.py` and deliberately not sent (`commands/steps/drive.md`), and a line whose
peer moved to `busy`/`shell` is **skipped** and reported under `Skipped:`. The record
carries the action, so `sent <M>` stays the count of messages that actually left while a
close or a reported skip does not masquerade as a miss:

    <recipient>                -> sent        (a bare name is a sent entry)
    sent<TAB><recipient>       -> sent
    closed<TAB><recipient>     -> closed through close-worker.py
    skipped<TAB><recipient>    -> reported under Skipped:, not sent

It does **not** prove delivery -- a send step that writes a name without calling
`SendMessage` defeats it. That limit is deliberate and named in the task's `# Out of
Scope`; the measured failure is a send leg that did not run, not one that lied.

Two halves, both required:

  **Static (always, no arguments).** The contract lives in three files and a later edit to
  any one of them can drop it silently:
    (a) `commands/steps/drive.md`'s send order names the `UNVERIFIABLE NUDGES` block.
    (b) `commands/steps/drive.md` writes the per-round sent record.
    (c) `commands/steps/drive.md` runs this check and prints its line.
    (d) `commands/steps/output-shape.md` mandates the line, so the round cannot omit it.

  **Artifacts (`--drive`, optional `--sent`).** Given a round's payload, count the drafted
  recipients, read the sent record, and fail on a deficit. This is the half the round runs.

Exit codes: 0 clean, 1 deficit, 2 refused (an unreadable payload or a malformed invocation
is not a clean round, and must never render as one).
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROUND = REPO / "commands" / "steps" / "drive.md"
OUTPUT_SHAPE = REPO / "commands" / "steps" / "output-shape.md"

#: Exit code for an input this gate will not measure. Distinct from 1 (a deficit).
REFUSED = 2

#: The line in `commands/steps/drive.md` that tells the round what to send. The block name
#: must appear on THIS line: the payload template names `UNVERIFIABLE NUDGES` too, and a
#: mention anywhere in the file would let the send order lose it while this stayed green.
SEND_ORDER_ANCHOR = "Send, from this session"
UNVERIFIABLE_BLOCK = "UNVERIFIABLE NUDGES"

#: The per-round sent record. Anchored on the `fleet-handback/<round>.sent` shape rather
#: than on `.sent`, so a passing mention of some other file cannot satisfy it.
SENT_RECORD_MARKER = "fleet-handback/<round>.sent"

#: This check's own name, which the print step must carry.
CHECK_NAME = "check-drive-nudges-sent.py"

#: The line `commands/steps/output-shape.md` must mandate. Anchored on the three counts
#: together, because `nudges:` alone is satisfied by any sentence about nudges.
OUTPUT_SHAPE_MARKER = "nudges: drafted <N> · sent <M> · missing <N-M>"

#: The blocks that draft a `TO:` line. `UNVERIFIABLE NUDGES` is here because its own
#: heading instructs "paste each line into NUDGES" -- a block that reaches the send step by
#: transcription cannot be left out of the denominator without the transcription dropping a
#: line out of it too.
COUNTED_BLOCKS = ("REAPS", "NUDGES", "UNVERIFIABLE NUDGES")

#: Column-0 section heads, longest first so `UNVERIFIABLE NUDGES` is matched before the
#: bare `UNVERIFIABLE` block that contains it. A head ends whatever block was open.
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

#: `TO: <recipient> | <message>`, optionally indented -- `drive-unverifiable.py` renders the
#: unverifiable block's lines with two leading spaces, and an agent pasting them strips them,
#: so both shapes are live.
TO_LINE = re.compile(r"^\s*TO:\s*(?P<recipient>.*?)\s*(?:\||$)")


def drafted_recipients(text):
    """The distinct `TO:` recipients the payload drafts, in first-seen order.

    Deduplicated by the recipient string, not by the line: the `UNVERIFIABLE NUDGES` block
    names a session the `NUDGES` block may already carry (measured `2026-11-11T08-51-29Z`:
    6 `TO:` lines, 5 recipients), and counting lines would report a deficit the round does
    not have. One recipient is one nudge.
    """
    seen = []
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
        if block not in COUNTED_BLOCKS:
            continue
        match = TO_LINE.match(line)
        if not match:
            continue
        recipient = match.group("recipient").strip()
        if recipient and recipient not in seen:
            seen.append(recipient)
    return seen


#: The outcomes a drafted line can have. `sent` is the only one that is a delivery; the
#: other two are deliberate and recorded, so neither is a miss.
ACTIONS = ("sent", "closed", "skipped")


def recorded_actions(path):
    """recipient -> action for the round's action record, or None when it is unreadable.

    A missing file is **nothing recorded, not an unknown** -- the record is per-round by
    design, so its absence is a statement about this round. `None` is reserved for a file
    that exists and cannot be read, which is a refusal rather than a clean zero.

    A bare name is a `sent` entry, so a plain list of the recipients that left still reads
    correctly instead of every line becoming an unparseable action.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        action, tab, recipient = entry.partition("\t")
        if tab and action.strip() in ACTIONS:
            out[recipient.strip()] = action.strip()
        else:
            out[entry] = "sent"
    return out


def sent_path_for(drive_path):
    """`<round>.sent` beside the payload: `.../2026-11-11T08-51-29Z.drive` -> `....sent`."""
    path = Path(drive_path)
    if path.suffix == ".drive":
        return path.with_suffix(".sent")
    return Path(str(path) + ".sent")


def static_failures():
    failures = []
    if not ROUND.exists():
        return ["%s does not exist -- the round's send order has no body to check" % ROUND]
    if not OUTPUT_SHAPE.exists():
        return [
            "%s does not exist -- the never-omitted rule has no body to check"
            % OUTPUT_SHAPE
        ]

    round_step = ROUND.read_text(encoding="utf-8")
    output_shape = OUTPUT_SHAPE.read_text(encoding="utf-8")

    # (a) the send order names the unverifiable block, on the send-order line itself.
    send_lines = [line for line in round_step.splitlines() if SEND_ORDER_ANCHOR in line]
    if not send_lines:
        failures.append(
            "%s no longer carries a `%s` line -- the send order is gone, so nothing tells "
            "the round what to send" % (ROUND, SEND_ORDER_ANCHOR)
        )
    elif not any(UNVERIFIABLE_BLOCK in line for line in send_lines):
        failures.append(
            "%s's send order no longer names the `%s` block -- the gate counts those lines "
            "and the send step is not told to send them, so a transcribed line is counted "
            "but never delivered" % (ROUND, UNVERIFIABLE_BLOCK)
        )

    # (b) the round writes the per-round sent record this gate reads.
    if SENT_RECORD_MARKER not in round_step:
        failures.append(
            "%s no longer writes `%s` -- the gate would read no record and report every "
            "drafted line missing" % (ROUND, SENT_RECORD_MARKER)
        )

    # (c) the round runs this check and prints its line.
    if CHECK_NAME not in round_step:
        failures.append(
            "%s no longer runs `%s` -- a round that drafted and sent nothing would pass "
            "unnoticed, which is the 2026-11-11 round" % (ROUND, CHECK_NAME)
        )

    # (d) the never-omitted rule mandates the line.
    if OUTPUT_SHAPE_MARKER not in output_shape:
        failures.append(
            "%s no longer mandates `%s` -- the round may compute the counts and render "
            "silence" % (OUTPUT_SHAPE, OUTPUT_SHAPE_MARKER)
        )
    return failures


def artifact_failures(drive_path, sent_path):
    """(failures, line) for one round's payload plus its sent record."""
    try:
        text = Path(drive_path).read_text(encoding="utf-8")
    except OSError as exc:
        return ["payload unreadable: %s" % exc], ""

    drafted = drafted_recipients(text)
    actions = recorded_actions(sent_path)
    if actions is None:
        return ["action record unreadable: %s" % sent_path], ""

    # Only a drafted recipient can be actioned. A recorded name outside the drafted set is
    # an over-send -- reported, never counted, because it would mask a deficit.
    sent = [name for name, action in actions.items() if action == "sent" and name in drafted]
    actioned = [name for name in actions if name in drafted]
    unexpected = [name for name in actions if name not in drafted]
    missing = len(drafted) - len(actioned)
    line = "nudges: drafted %d · sent %d · missing %d" % (
        len(drafted),
        len(sent),
        missing,
    )
    if unexpected:
        line += " · unexpected sent %d" % len(unexpected)
    if missing:
        return (
            [
                "%d of %d drafted nudge(s) have no sent, closed or skipped entry -- the "
                "ledger stamps them as nudged, so the next round suppresses them and step "
                "5b closes the row" % (missing, len(drafted))
            ],
            line,
        )
    return [], line


def _parse(argv):
    """(drive, sent, error). Accepts `--flag <path>` and `--flag=<path>`.

    An unknown flag or a positional argument is an error rather than an absence: skipping
    it silently would fall through to the static-only run and exit 0 over an unchecked
    round. No arguments at all is the Makefile/CI static run and is legal.
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
        print("check-drive-nudges-sent: refused -- %s" % parse_error, file=sys.stderr)
        return REFUSED

    line = ""
    if drive_path:
        artifact_failure, line = artifact_failures(
            drive_path, sent_path or sent_path_for(drive_path)
        )
        failures.extend(artifact_failure)
        if line:
            print(line)
        elif failures:
            print("nudges: refused -- see the failures below")

    if failures:
        print("check-drive-nudges-sent: FAIL", file=sys.stderr)
        for failure in failures:
            print("  - %s" % failure, file=sys.stderr)
        return 1

    scope = "static contract"
    if drive_path:
        scope += " + payload %s" % drive_path
    print(
        "drive-nudges-sent ok: the send order names the unverifiable block, the round writes "
        "its sent record and runs this check, and output-shape mandates the line (%s)" % scope
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
