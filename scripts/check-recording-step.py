#!/usr/bin/env python3
"""Every manager command that can run the recording block must reach it from its own body.

`docs/subject-resolution.md` § Recording is the rule's one home. Three commands resolve a
subject **and** hold the tools to run its block — `manager-loop`, `manager-status`,
`manager-drive`. Each must carry the step in its own body, because a pointer alone is
exactly what failed: the manager reads the command it is about to run, not the document
that command delegates to, and `commands/manager-loop.md` carried **0** occurrences of
`worker-manager` while the write sat one hop away in the doc.

Same shape as `check-spawn-mode.py`, for the same reason — a contract with several call
sites needs a mechanical check, not a keep-in-sync sentence. The repo already learned
that: `docs/subject-resolution.md` records that the *copy* was the liability when four
commands each carried the recording contract, which is why the rules live in one file now.

Three assertions, all required:

  (a) each command **carries the step** — the marker paragraph is present, and it names
      the file it writes (`~/.claude/state/worker-manager/<session-id>.json`)
  (b) each step **references the home** for the rules — a pointer, not a copy of them
  (c) the three step blocks are **identical apart from three enumerated slots** — the point
      in each command's own flow where the step runs (`the sweep` / `the report` / `the
      gate`), the verb in the read-back line (`arm` / `report` / `drive`), and the moment
      the read-back names (`arm-time` for the command that arms a loop, `read-time` for the
      two that do not). All three are command-specific by design and carry real
      information; everything else must match, so a fourth divergence cannot appear
      silently

`manager-verify` is excluded **by design**, not by omission: its trimmed `allowed-tools`
omits `Bash(python3:*)`, `Bash(mkdir:*)` and `Bash(vault-cli:*)`, so it can run neither
the vault lookup nor the recording block, and it records nothing. That divergence is
documented in its own body and in `docs/subject-resolution.md` § Recording.

**What this check does not prove.** A command file is prose — there is no function to
call and no return value to thread. So it proves the step cannot be silently *omitted*
or *diverged*; it cannot prove the step *runs*. That half is behavioural — the
registration file appearing in `~/.claude/state/worker-manager/` on a real arm — and no
grep can fake it.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Commands that resolve a subject AND can run the recording block.
COMMANDS = [
    "commands/manager-loop.md",
    "commands/manager-status.md",
    "commands/manager-drive.md",
]

# Commands that resolve a subject and must NOT carry the step, with the reason.
EXCLUDED = {
    "commands/manager-verify.md": "read-only re-scope trimmed its allowed-tools; it records nothing",
}

MARKER = "**⚠️ Record the resolution — a step to run, not a reference to follow.**"
HOME = "docs/subject-resolution.md"
WRITES = "~/.claude/state/worker-manager/<CLAUDE_CODE_SESSION_ID>.json"


def step_block(text: str, path: str) -> str | None:
    """The marker paragraph through to the next `## ` heading — the step and its rationale."""
    start = text.find(MARKER)
    if start == -1:
        return None
    end = text.find("\n## ", start)
    return text[start:] if end == -1 else text[start:end]


# The three slots that are command-specific by design. Everything else in the block must
# match across the three commands; these are folded to a placeholder before comparing, so
# a fourth divergence — the thing that actually drifts — still fails the check.
SLOTS = [
    (re.compile(r"before the (sweep|report|gate)\b"), "before the <STEP>"),
    (re.compile(r"Never (arm|report|drive) quietly\b"), "Never <VERB> quietly"),
    (re.compile(r"This is the (arm|read)-time signal\b"), "This is the <WHEN>-time signal"),
]


def normalize(block: str) -> str:
    for pattern, replacement in SLOTS:
        block = pattern.sub(replacement, block)
    return block


def main() -> int:
    failures: list[str] = []
    blocks: dict[str, str] = {}

    for rel in COMMANDS:
        path = REPO / rel
        if not path.is_file():
            failures.append(f"{rel}: missing")
            continue
        text = path.read_text(encoding="utf-8")

        block = step_block(text, rel)
        if block is None:
            failures.append(
                f"{rel}: (a) does not carry the recording step — the marker paragraph is absent, "
                f"so this command can resolve a subject and never register it"
            )
            continue
        blocks[rel] = block

        if WRITES not in block:
            failures.append(
                f"{rel}: (a) the step does not name the file it writes ({WRITES}) — "
                f"a step that does not say what it writes cannot be read back"
            )
        if HOME not in block:
            failures.append(
                f"{rel}: (b) the step does not reference its home ({HOME}) — a copy of the "
                f"rules rather than a pointer to them is the defect this check exists to catch"
            )

    for rel, reason in EXCLUDED.items():
        path = REPO / rel
        if path.is_file() and MARKER in path.read_text(encoding="utf-8"):
            failures.append(
                f"{rel}: carries the recording step but is excluded by design ({reason}) — "
                f"either the exclusion or the step is wrong"
            )

    if len(blocks) > 1:
        first_rel, first = next(iter(blocks.items()))
        first_norm = normalize(first)
        for rel, block in blocks.items():
            if normalize(block) != first_norm:
                failures.append(
                    f"{rel}: (c) its step block differs from {first_rel}'s outside the three "
                    f"enumerated slots — the copies have drifted; edit all three together"
                )

    if failures:
        print("check-recording-step: FAIL", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"recording-step ok: {len(blocks)} command(s) carry the step, all reference "
        f"{HOME} § Recording, and the blocks match outside the three enumerated slots "
        f"({len(EXCLUDED)} excluded by design)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
