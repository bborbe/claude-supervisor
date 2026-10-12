#!/usr/bin/env python3
"""The fleet-drive ledger has exactly one writer, and it is a script.

`agents/fleet-drive.md` step 7 used to merge `~/.claude/state/fleet-drive/ledger.json`
with a hand-written jq expression. Measured 2026-10-12: a precedence bug in one round
collapsed the file from 376 entries to 1, and the re-nudge/re-reap suppression history
-- the whole reason the file exists -- went with it. The prose said "Merge, never
replace" and had said it for months; prose has nothing to run against it.

The merge now lives in `scripts/drive-ledger.py`, and this is the gate that keeps it
there. Same shape, and the same reason, as `check-subject-write-scope.py` and
`check-recording-step.py` -- *"a contract with several call sites needs a mechanical
check, not a keep-in-sync sentence"*. The difference here is that the contract has ONE
call site and the failure mode is a silent data loss, so the check is about the write
path rather than about keeping copies in step.

Three assertions, all required:

  (a) `agents/fleet-drive.md` **names the script** at least twice -- a read and a write.
      This is the only assertion that fails on the tree as it stood before the change
      (measured 2026-10-12: `drive-ledger\\.py` -> 0), so it is the one that makes the
      gate worth running rather than vacuously green.
  (b) `ledger.json` appears **exactly once** across `agents/` and `commands/`, in
      `agents/fleet-drive.md`, as the `LEDGER  ~/.claude/state/...` output-format line.
      The script owns the path, so prose naming it again is a hand-rolled write waiting
      to happen. (Measured 2026-10-12, LINES: 3 before the change, 1 after -- 5
      occurrences over those 3 lines, since step 7 named both `ledger.json` and
      `ledger.json.tmp`. The count is stated as lines because that is what a
      `grep -c` reports, and the two measures disagreeing is what made this
      sentence ambiguous once already.)
  (c) **No line that references the ledger also WRITES it through an inline
      interpreter** -- no `jq` redirect, no `python3 -c` that dumps JSON, no heredoc
      into a shell. This is the clause that would have caught the original wipe.

      ⚠️ **A write signal is required, not just an interpreter token, and the first
      draft got this wrong.** The unscoped-token version flagged this repo's own step 7
      the moment it was rewritten, because that line forbids the practice -- *"Never
      write the ledger yourself -- no `jq`, no `python3 -c`"* -- and so names `jq` and
      `python3 -c` on a line that mentions the ledger. A rule that cannot tell a
      prohibition from an instruction is a rule people learn to word around, and the
      wording it would force (never name the tool you are forbidding) is strictly worse
      prose. The threat is data LOSS, which needs a write: a `jq` that only reads the
      ledger is harmless, so requiring a redirect, a `tee`/`sponge`, or a JSON-dumping
      call is both faithful to the incident and quiet on prohibitions.

⚠️ **Clause (c) is scoped to ledger-referencing lines on purpose, and the scope is the
whole point.** Run unscoped across `agents/` + `commands/`, the same pattern has 12
matches at v0.133.0 -- `commands/open.md` (6), `agents/manager-drive.md` (2),
`agents/manager-sweep-reader.md`, `commands/manager-status.md`,
`commands/steps/manager-resolve.md`, `commands/worker-drive.md` -- none of them about
this ledger. An unscoped "0 matches" can never exit 0 and would block every change to
the repo behind a criterion no one can meet. (22 repo-wide; 12 is the count for the two
directories this check scans.)

⚠️ **The pattern is written for Python's `re`, not for POSIX ERE.** The equivalent
`grep -E` form is `(^|[^[:alnum:]_])(jq|tee)([^[:alnum:]_]|$)`; Python's `re` has no
POSIX character classes, so the negation is spelled `[^A-Za-z0-9_]` here. Neither form
may use `\\b`: git's ERE does not implement it and it matches NOTHING, so a zero-hit
sweep reads as a pass. Proven on this repo 2026-10-12 -- on `scripts/asked-ledger.py`,
`argparse` -> 3 hits, `\\bargparse\\b` -> 0.

**What this check does not prove.** It reads what the files *say*. A `drive-ledger.py`
that ignores its stdin, or an agent that shells out to jq anyway, passes here and fails
at runtime -- that half is behavioural, and `scripts/tests/test_drive_ledger.py` is
where the script's own merge is actually exercised.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "agents"
COMMANDS = REPO / "commands"

#: The one file allowed to name the script.
DRIVE = "agents/fleet-drive.md"

#: The script's name, as the agent must call it.
SCRIPT = "drive-ledger.py"
MIN_SCRIPT_MENTIONS = 2  # a read and a write

#: The path literal, allowed exactly once -- in the rendered `LEDGER ...` report line.
PATH_LITERAL = "ledger.json"
PATH_MENTIONS_ALLOWED = 1

#: An inline interpreter, in the `grep -E` spelling the docstring quotes.
INLINE_INTERPRETER = re.compile(
    r"(^|[^A-Za-z0-9_])(jq|tee)([^A-Za-z0-9_]|$)|python3? -c|python3? <<"
)

#: A line is in scope for clause (c) when it is about this ledger.
LEDGER = re.compile(r"ledger", re.I)

#: ...and only when it shows evidence of WRITING it. See the docstring: without this,
#: the rule flags the prohibition that names the forbidden tools, which is the one line
#: in the file most obviously not committing the offence.
#: ⚠️ The redirect is `\s>` and NOT a bare `>`. This file is full of `<n>`, `<ts>` and
#: `<SECTION>` placeholders, so a bare `>` matches the closing angle bracket of an
#: ordinary placeholder -- measured 2026-10-12, where it flagged step 7 on the `>` in
#: `"<ts>"`. A redirect is preceded by whitespace; a placeholder's bracket is not.
WRITE_SIGNAL = re.compile(
    r"\s>|\|\s*tee\b|\bsponge\b|\bjson\.dump|\bos\.replace\b|\bshutil\.(copy|move)\b|\.write\("
)


def markdown_files():
    """Every agent and command body, `commands/steps/*.md` included."""
    return sorted(AGENTS.glob("*.md")) + sorted(COMMANDS.rglob("*.md"))


def rel(path):
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def main():
    failures = []
    files = markdown_files()

    # ⚠️ A scan that found no files must not read as a clean sweep. An empty glob means
    # the root is wrong, which is a different failure from "nothing matched".
    if not files:
        print("check-drive-ledger-writes: FAIL", file=sys.stderr)
        print(
            "  - no markdown under %s/agents or %s/commands -- the scan root is wrong, "
            "not the tree clean" % (REPO, REPO),
            file=sys.stderr,
        )
        return 1

    drive = REPO / DRIVE
    if not drive.exists():
        print("check-drive-ledger-writes: FAIL", file=sys.stderr)
        print("  - %s does not exist -- the drive leg has no body to check" % DRIVE, file=sys.stderr)
        return 1

    # (a) the script is actually wired in.
    mentions = drive.read_text(encoding="utf-8").count(SCRIPT)
    if mentions < MIN_SCRIPT_MENTIONS:
        failures.append(
            "%s names `%s` %d time(s), fewer than the %d a read and a write need -- the "
            "ledger is being written some other way" % (DRIVE, SCRIPT, mentions, MIN_SCRIPT_MENTIONS)
        )

    # (b) exactly one path literal, and it is the report line.
    hits = []
    for path in files:
        count = path.read_text(encoding="utf-8").count(PATH_LITERAL)
        if count:
            hits.append((rel(path), count))
    total = sum(count for _name, count in hits)
    if total != PATH_MENTIONS_ALLOWED:
        where = ", ".join("%s (%d)" % (name, count) for name, count in hits) or "nowhere"
        failures.append(
            "`%s` appears %d time(s) across agents/ and commands/ (%s), not the %d allowed -- "
            "the script owns the path, so prose naming it again is a hand-rolled write"
            % (PATH_LITERAL, total, where, PATH_MENTIONS_ALLOWED)
        )
    elif hits[0][0] != DRIVE:
        failures.append(
            "the one `%s` mention is in %s, not %s -- the rendered report line belongs to "
            "the drive leg" % (PATH_LITERAL, hits[0][0], DRIVE)
        )

    # (c) nothing writes the ledger through an inline interpreter.
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if (
                LEDGER.search(line)
                and INLINE_INTERPRETER.search(line)
                and WRITE_SIGNAL.search(line)
            ):
                failures.append(
                    "%s:%d writes the ledger through an inline interpreter: %s"
                    % (rel(path), number, line.strip())
                )

    if failures:
        print("check-drive-ledger-writes: FAIL", file=sys.stderr)
        for failure in failures:
            print("  - %s" % failure, file=sys.stderr)
        return 1

    print(
        "drive-ledger-writes ok: %s names %s %d time(s), `%s` appears once (the report "
        "line), and no ledger line reaches an inline interpreter across %d file(s)"
        % (DRIVE, SCRIPT, mentions, PATH_LITERAL, len(files))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
