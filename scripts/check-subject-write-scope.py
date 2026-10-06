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
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.S)

# The imperative the writers carry. Deliberately the full phrase: a bare mention of the
# section is what the non-writers carry, so it cannot be the discriminator.
IMPERATIVE = "§ *Reconcile the subject's status* **now**"
# The two reasons a non-writer's row may state. Every `no` row must carry one, so a reworded
# cell fails the check rather than silently skipping the grant assertion keyed on it.
REASONS = ("holds no `Bash(vault-cli:*)`", "read-only by its own contract")
# The binaries § Reconcile the subject's status guarantees every writer is granted. `awk` reads
# the page and `vault-cli` writes it; the section states both, so both are asserted.
WRITER_GRANTS = ("Bash(awk:*)", "Bash(vault-cli:*)")
PARAGRAPH_MARKER = "**⚠️ Reconcile the subject's status — a step to run, not a reference to follow.**"
STEP_SLOT = re.compile(r"before the (sweep|gate)\b")


def paragraph(text: str) -> str | None:
    """The invocation paragraph — bounded at the next blank line, marker or heading.

    Every bound is applied, not just the first found: a marker in a file's last paragraph
    would otherwise return to end-of-file, and a truncated chunk that happened to match the
    other writer's full one would mask real drift instead of failing loudly.
    """
    start = text.find(PARAGRAPH_MARKER)
    if start == -1:
        return None
    end = len(text)
    for bound in (
        text.find("\n\n", start),
        text.find("\n**⚠️ ", start),
        text.find("\n## ", start),
    ):
        if bound != -1:
            end = min(end, bound)
    return text[start:end]


def main() -> int:
    failures: list[str] = []

    doc = (REPO / DOC).read_text(encoding="utf-8")
    table = TABLE_ROW.findall(doc)
    rows = {name: verdict for name, verdict, _ in table}

    # Compare the parsed *rows*, not the name-keyed dict: a duplicated row collapses in the
    # dict, so a table naming one command twice would pass a dict-length check. Both halves
    # are needed — the row count catches an added row, the distinct-name count a swapped one.
    if len(table) != 4 or len(rows) != len(table):
        failures.append(
            f"{DOC}: the write-scope table has {len(table)} row(s) naming {len(rows)} distinct "
            f"command(s), expected 4 of each — a table that does not enumerate every resolving "
            f"command exactly once cannot be checked"
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
            # A pointer, not a copy. Dropping the home from *both* writers passes the equality
            # check and the invocation assertion together, so it is asserted separately — the
            # same discipline the sibling guard's assertion (b) applies to the recording step.
            if DOC not in para:
                failures.append(
                    f"{rel}: its invocation paragraph does not name its home ({DOC}) — the "
                    f"writers' paragraphs matching is not enough if they both stopped pointing "
                    f"at the rule"
                )
                continue
            # Assert the fold actually fired. Without this a third step word degrades the
            # comparison to a strict literal, and the invariant silently stops being checked
            # in the direction that matters — a shared word is not drift, but an unchecked
            # one is not a check.
            if STEP_SLOT.search(para) is None:
                failures.append(
                    f"{rel}: its invocation paragraph carries no step word matching "
                    f"{STEP_SLOT.pattern} — the fold never fired, so the two writers are being "
                    f"compared as literals"
                )
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

    # The table's stated *reason* is a claim in its own right, and it is what the grant
    # assertion is keyed on. It is **derived from the frontmatter** rather than matched against
    # a set of accepted strings: accepting either reason let a row reworded to the other one
    # clear this gate and then skip the grant assertion entirely — the silent skip this loop
    # exists to prevent, reachable by editing prose.
    for name, verdict, tail in table:
        if verdict != "no":
            continue
        rel = f"commands/{name}.md"
        path = REPO / rel
        if not path.is_file():
            continue
        # Fail closed: a frontmatter block this regex cannot read is a block whose grants were
        # never checked, and a skipped assertion reports green against text it does not cover.
        front = FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if front is None:
            failures.append(
                f"{rel}: its frontmatter could not be read, so the table's claim about it was "
                f"never checked — failing closed rather than passing vacuously"
            )
            continue
        # A non-writer barred by a *missing grant* states the grant; one that holds the grant
        # and is barred by contract states the contract. The frontmatter decides which applies,
        # so the row cannot choose the reason that skips the assertion.
        holds = "Bash(vault-cli:*)" in front.group(1)
        expected = REASONS[1] if holds else REASONS[0]
        if expected not in tail:
            failures.append(
                f"{rel}: the table calls it a non-writer, but its row does not state the reason "
                f"its frontmatter implies — expected `{expected}`, since it "
                f"{'holds' if holds else 'holds no'} `Bash(vault-cli:*)`"
            )

    # § Reconcile the subject's status guarantees the commands it names use only binaries both
    # writers are granted — `awk` and `vault-cli`. Assert it rather than trusting the prose:
    # this guarantee shipped false for `manager-drive`, which carried the invocation without
    # the grant, and the non-writer pass below checks only the *absence* of a grant.
    for name, verdict, _tail in table:
        if verdict != "yes":
            continue
        rel = f"commands/{name}.md"
        path = REPO / rel
        if not path.is_file():
            continue
        front = FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if front is None:
            failures.append(
                f"{rel}: the table calls it a writer, but its frontmatter could not be read, "
                f"so its grants were never checked"
            )
            continue
        for grant in WRITER_GRANTS:
            if grant not in front.group(1):
                failures.append(
                    f"{rel}: the table calls it a writer, but its allowed-tools omits "
                    f"`{grant}` — the section guarantees the writers are granted the binaries "
                    f"the rule needs, and a pipeline reaching for an ungranted one parks on an "
                    f"approval the command was never scoped for"
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
