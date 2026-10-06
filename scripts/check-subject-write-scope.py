#!/usr/bin/env python3
"""The reconcile's write-scope table is a contract with four call sites, so it needs a check.

`docs/subject-resolution.md` § Reconcile the subject's status carries a table naming which
of the four resolving commands may flip a subject's `status`. Nothing enforced it: if
`/manager-verify` ever regained `Bash(vault-cli:*)`, or a writer silently lost its
invocation, the table would become false with no gate failing.

Same shape, and the same reason, as `check-recording-step.py` — *"a contract with several
call sites needs a mechanical check, not a keep-in-sync sentence"*. The immediately
preceding change added `check-content-key-formula.py` on the same grounds.

Three assertions, all required:

  (a) the table names all four resolving commands, and each row's verdict matches what that
      command's body actually does — the two writers carry the imperative invocation, the
      two non-writers do not
  (b) every command the table calls a non-writer still **points at** the section, so its
      silence reads as a stated carve-out rather than a missing step
  (c) the two writers' invocation paragraphs are identical apart from the step they run
      before (`the sweep` / `the gate`) — the same fold-then-compare discipline
      `check-recording-step.py` applies to the recording step, so a third divergence
      cannot appear silently

**What this check does not prove.** A command file is prose — there is no function to call
and no return value to thread. So it proves the table and the bodies cannot drift apart
unnoticed; it cannot prove the write runs. That half is behavioural.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DOC = "docs/subject-resolution.md"
TABLE_ROW = re.compile(r"^\|\s*`/(manager-[a-z]+)`\s*\|\s*yes\s*\|\s*\*\*(yes|no)\*\*(.*)$", re.M)
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)

# The imperative the writers carry. Deliberately the full phrase: a bare mention of the
# section is what the non-writers carry, so it cannot be the discriminator.
IMPERATIVE = "§ *Reconcile the subject's status* **now**"
PARAGRAPH_MARKER = "**⚠️ Reconcile the subject's status — a step to run, not a reference to follow.**"
STEP_SLOT = re.compile(r"before the (sweep|gate)\b")


def paragraph(text: str) -> str | None:
    """The invocation paragraph — marker to the next blank line."""
    start = text.find(PARAGRAPH_MARKER)
    if start == -1:
        return None
    end = text.find("\n\n", start)
    return text[start:] if end == -1 else text[start:end]


def main() -> int:
    failures: list[str] = []

    doc = (REPO / DOC).read_text(encoding="utf-8")
    table = TABLE_ROW.findall(doc)
    rows = {name: verdict for name, verdict, _ in table}

    if len(rows) != 4:
        failures.append(
            f"{DOC}: the write-scope table names {len(rows)} command(s), expected 4 — "
            f"a table that does not enumerate every resolving command cannot be checked"
        )

    writers: dict[str, str] = {}
    for name, verdict in rows.items():
        rel = f"commands/{name}.md"
        path = REPO / rel
        if not path.is_file():
            failures.append(f"{rel}: named in the table but missing")
            continue
        text = path.read_text(encoding="utf-8")
        carries = IMPERATIVE in text

        if verdict == "yes":
            if not carries:
                failures.append(
                    f"{rel}: the table calls it a writer, but its body carries no reconcile "
                    f"invocation — the rule would never fire"
                )
                continue
            para = paragraph(text)
            if para is None:
                failures.append(f"{rel}: carries the invocation but not the shared paragraph marker")
                continue
            writers[rel] = STEP_SLOT.sub("before the <STEP>", para)
        else:
            if carries:
                failures.append(
                    f"{rel}: the table calls it a non-writer, but its body carries the "
                    f"imperative reconcile invocation — the file would instruct a vault write "
                    f"it is not permitted to make"
                )
            if "Reconcile the subject's status" not in text:
                failures.append(
                    f"{rel}: a non-writer that never names the section — its silence reads as a "
                    f"missing step rather than a stated carve-out"
                )

    # The table's stated *reason* is a claim in its own right. Where it names a missing
    # grant, assert the command really lacks it — that is the half that goes stale silently
    # if the grant ever returns, which is the failure this whole check exists to catch.
    for name, _verdict, tail in table:
        if "holds no `Bash(vault-cli:*)`" not in tail:
            continue
        rel = f"commands/{name}.md"
        path = REPO / rel
        if not path.is_file():
            continue
        front = FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if front and "Bash(vault-cli:*)" in front.group(1):
            failures.append(
                f"{rel}: the table says it holds no `Bash(vault-cli:*)`, but its allowed-tools "
                f"grants it — the stated reason is stale"
            )

    if len(writers) > 1:
        first_rel, first = next(iter(writers.items()))
        for rel, para in writers.items():
            if para != first:
                failures.append(
                    f"{rel}: its invocation paragraph differs from {first_rel}'s outside the "
                    f"step slot — the copies have drifted; edit both together"
                )

    if failures:
        print("check-subject-write-scope: FAIL", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"subject-write-scope ok: the table names {len(rows)} command(s), "
        f"{len(writers)} writer(s) carry the invocation, the rest carry a stated carve-out, "
        f"and the writers' paragraphs match outside the step slot"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
