#!/usr/bin/env python3
"""The drive round's `UNVERIFIABLE` block must reach BOTH of the round's artifacts.

Measured 2026-10-29 on round `T1358`: `grep -c UNVERIFIABLE` was **0** in both
`~/.claude/state/fleet-handback/2026-10-29T1358.digest` and `…T1358.drive`, while
`scripts/drive-unverifiable.py` had produced rows and the agent had pasted the nudge and
escalation lines into its payload. The block itself had no home: the payload template
enumerated its sections and `UNVERIFIABLE` was not one of them, and the round's print step
enumerated what it prints and the block was not one of those either. Absence rendered
exactly like "nothing was unverifiable" -- the false negative this goal exists to remove.

Two halves, both required:

  **Static (always).** The contract that makes a round carry the block is in three files,
  and a later edit to any one of them can drop it without anything else noticing:
    (a) `agents/fleet-drive.md`'s payload template carries an `UNVERIFIABLE` section.
    (b) `agents/fleet-drive.md` step 6b runs `drive-unverifiable.py handle --digest`.
    (c) `commands/steps/drive.md` prints the block and names this check.
    (d) `scripts/drive-unverifiable.py` exists and `render()` still emits the
        `UNVERIFIABLE ESCALATIONS` producer line this block is built around.

  **Artifacts (`--digest` + `--drive`).** Given a round's two files, both must carry the
  block and the blocks must agree. This is the half a deliberately-broken run trips: a
  payload that lost the paste, or a digest whose mirror never landed, fails here while the
  static half stays green.

**What this does not prove.** It reads what the files *say* and what a given pair of
artifacts *carry*. It cannot prove an agent executed the paste on a future round -- that is
why the artifact half is a runnable gate the round itself invokes, not a CI-only check.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT = REPO / "agents" / "fleet-drive.md"
ROUND = REPO / "commands" / "steps" / "drive.md"
SCRIPT = REPO / "scripts" / "drive-unverifiable.py"

#: The heading the payload template must carry, and the name the round's print step must
#: use. The round names the block; the template carries its heading.
BLOCK_HEADING = "UNVERIFIABLE —"
BLOCK_NAME = "`UNVERIFIABLE` block"

#: The template's own section line. Anchored on the whole sentence, not on `UNVERIFIABLE —`
#: alone: the template's row placeholder also carries that prefix, so a heading check would
#: stay green with the section itself deleted.
TEMPLATE_ANCHOR = "UNVERIFIABLE — `drive-unverifiable.py handle`'s block, pasted verbatim"

#: The step that invokes the producer. Anchored on `--entries`, which only the step-6b
#: invocation carries -- the write-surface rule names the same command with `--digest` and
#: no `--entries`, and a looser anchor would let that mention keep this check green while
#: the step itself lost the flag.
PRODUCER_CALL = "drive-unverifiable.py handle --entries"

#: The flag as the invocation spells it, in position. A prose mention of `--digest` on the
#: same line (step 6b explains the mirror in its own words) must not satisfy the check, so
#: this is the argument pair, not the flag token.
PRODUCER_FLAG = "--json-out /tmp/drive-round.json --digest"

#: The producer line `render()` builds the escalation half around.
PRODUCER_LINE = "UNVERIFIABLE ESCALATIONS —"

#: Column-0 headings that end the drive payload's `UNVERIFIABLE` section. The block's own
#: `UNVERIFIABLE NUDGES` / `UNVERIFIABLE ESCALATIONS` lines are column-0 too, so they are
#: matched BEFORE this set (they start with the block's own prefix) and never terminate it.
NEXT_SECTION = re.compile(
    r"^(REAPS|NUDGES|FREEABLE TABS|Waiting on your keystroke|LEDGER|RETAINED —|PARKED —|"
    r"ESCALATION —)"
)

#: Markers `drive-unverifiable.py write_digest()` wraps the mirror in.
DIGEST_BEGIN = "<!-- drive-unverifiable:begin -->"
DIGEST_END = "<!-- drive-unverifiable:end -->"


def drive_block(text):
    """The payload's `UNVERIFIABLE` section, or "" when the payload has none."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(BLOCK_HEADING)), None
    )
    if start is None:
        return ""
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("UNVERIFIABLE "):
            out.append(line)
            continue
        if NEXT_SECTION.match(line):
            break
        out.append(line)
    return "\n".join(out).strip()


def digest_block(text):
    """The mirrored block between the markers, or "" when the digest carries none."""
    head, sep, rest = text.partition(DIGEST_BEGIN)
    if not sep:
        return ""
    block, end, _ = rest.partition(DIGEST_END)
    if not end:
        return ""
    return block.strip()


def _label(path):
    """`path` relative to the repo when it sits inside it, else the path itself.

    The tests point the module at a broken COPY of the tree, outside `REPO`; a bare
    `relative_to` raises there instead of reporting the failure it was called to build.
    """
    try:
        return str(Path(path).relative_to(REPO))
    except ValueError:
        return str(path)


def static_failures():
    failures = []
    if not AGENT.exists():
        return ["%s does not exist -- the payload template has no body to check" % AGENT]
    if not ROUND.exists():
        return ["%s does not exist -- the round's print step has no body to check" % ROUND]
    if not SCRIPT.exists():
        return ["%s does not exist -- the block's producer is missing" % SCRIPT]

    agent = AGENT.read_text(encoding="utf-8")
    round_step = ROUND.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    # (a) the payload template has a section for the block.
    if TEMPLATE_ANCHOR not in agent:
        failures.append(
            "%s carries no `%s` section in its payload template -- the agent has nowhere "
            "to paste the block, which is exactly the 2026-10-29 round-T1358 defect"
            % (_label(AGENT), BLOCK_HEADING)
        )

    # (b) the producer is invoked with the digest mirror. Read the invoking line itself: a
    # mention of `--digest` anywhere in the file (the write-surface rule names it too) would
    # let the step lose the flag while this stayed green.
    call_lines = [line for line in agent.splitlines() if PRODUCER_CALL in line]
    if not call_lines:
        failures.append(
            "%s no longer invokes `%s` at all" % (_label(AGENT), PRODUCER_CALL)
        )
    elif not any(PRODUCER_FLAG in line for line in call_lines):
        failures.append(
            "%s step 6b no longer passes `--digest` to drive-unverifiable.py -- the round's "
            "digest stops carrying the block, and only the .drive does" % _label(AGENT)
        )

    # (c) the round prints the block and names this gate.
    if BLOCK_NAME not in round_step:
        failures.append(
            "%s no longer names the %s in its print step -- the payload may carry it while "
            "the round renders silence" % (_label(ROUND), BLOCK_NAME)
        )
    if "check-drive-unverifiable-paste.py" not in round_step:
        failures.append(
            "%s no longer runs this check -- a round that lost the block would pass "
            "unnoticed" % _label(ROUND)
        )

    # (d) the producer line and the digest flag survive in the script.
    if PRODUCER_LINE not in script:
        failures.append(
            "%s no longer emits `%s` -- the block the paste sites carry has no producer"
            % (_label(SCRIPT), PRODUCER_LINE)
        )
    if '"--digest"' not in script:
        failures.append(
            "%s no longer declares `--digest` -- the mirror cannot be written"
            % _label(SCRIPT)
        )
    return failures


def artifact_failures(digest_path, drive_path):
    failures = []
    for path, extract, label in (
        (digest_path, digest_block, "digest"),
        (drive_path, drive_block, "drive"),
    ):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            failures.append("%s artifact unreadable: %s" % (label, exc))
            continue
        if not extract(text):
            failures.append(
                "%s artifact carries no UNVERIFIABLE block: %s -- a round that lost the "
                "block and a round where nothing was unverifiable must never read the same"
                % (label, path)
            )
    if failures:
        return failures
    digest_text = Path(digest_path).read_text(encoding="utf-8")
    drive_text = Path(drive_path).read_text(encoding="utf-8")
    if digest_block(digest_text) != drive_block(drive_text):
        failures.append(
            "the two artifacts' UNVERIFIABLE blocks disagree -- digest %s, drive %s"
            % (digest_path, drive_path)
        )
    return failures


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    digest_path = None
    drive_path = None
    if "--digest" in argv:
        i = argv.index("--digest")
        digest_path = argv[i + 1] if i + 1 < len(argv) else None
    if "--drive" in argv:
        i = argv.index("--drive")
        drive_path = argv[i + 1] if i + 1 < len(argv) else None

    failures = static_failures()
    if bool(digest_path) != bool(drive_path):
        failures.append(
            "--digest and --drive must be given together: this check verifies the round's "
            "PAIR, and a single file cannot show that both agree"
        )
    if digest_path and drive_path:
        failures.extend(artifact_failures(digest_path, drive_path))

    if failures:
        print("check-drive-unverifiable-paste: FAIL", file=sys.stderr)
        for failure in failures:
            print("  - %s" % failure, file=sys.stderr)
        return 1

    scope = "static contract"
    if digest_path:
        scope += " + artifacts %s / %s" % (digest_path, drive_path)
    print(
        "drive-unverifiable-paste ok: payload template, step 6b's --digest, the round's "
        "print step, the producer line and the script's --digest all present (%s)" % scope
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
