#!/usr/bin/env python3
"""Emit the `/goal` condition for a task row, with its Success Criteria inlined.

The `/goal` evaluator is a small tool-less model that reads the transcript only. A
condition saying "the task's SC are met" leaves it guessing what "met" means, so the
manager reads the task file and inlines each criterion — the judge then never needs a
file read. The disk probe stays in every form, so a criterion's text drifting out from
under the goal cannot manufacture a false "met".

An inlined criterion is never proof. Every form says so: a criterion counts only when
its evidence (a command's output, a PR, a link) is printed in a recent turn. That
sentence is the whole defence against a worker asserting a criterion is met while the
transcript shows nothing.

Three forms, chosen by size — the command caps a condition at 4,000 chars:

  full     every SC verbatim, one per line
  compact  each SC cut to its first sentence
  probe    no SC text at all — the disk probe plus the SC count

The ladder is not decoration: a task with many criteria overruns the cap in `full` and
the alternative to compacting is a condition the command refuses outright.

Usage:
  goal-condition.py --task "<name>" --vault <v> [--max-chars 4000] [--compact-at 3600]

Prints the condition on stdout and the chosen form on stderr, so a spawn record can
note when the ladder fell back.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys

GATE_STOP = (
    "Also stop — without ticking any box — when the last turn ends with an operator ask\n"
    "(approve:/pick/you run:/review:), a permission is denied, the task reads hold/aborted,\n"
    "or 40 turns have passed."
)


def task_file(name, vault):
    """Absolute path of the task file, or None when vault-cli cannot resolve it.

    Resolved through `task show --output json` rather than a glob: dated task names
    collide across vaults, and a name glob silently picks the wrong file.
    """
    try:
        out = subprocess.run(
            ["vault-cli", "task", "show", name, "--vault", vault, "--output", "json"],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out).get("file_path") or None
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def criteria(text):
    """The `# Success Criteria` bullets, in file order, as (id, body).

    Both tick states are kept: the condition states the whole definition of done, and
    the disk probe — not this list — decides whether the boxes are actually ticked.
    """
    found, inside = [], False
    for line in text.splitlines():
        if line.startswith("# Success Criteria"):
            inside = True
            continue
        if inside and line.startswith("# "):
            break
        if not inside:
            continue
        match = re.match(r"^\s*-\s*\[[ x/]\]\s*(.+)$", line)
        if not match:
            continue
        body = match.group(1).strip()
        ident, _, rest = body.partition(":")
        if rest and re.fullmatch(r"SC\d+", ident.strip()):
            found.append((ident.strip(), rest.strip()))
        else:
            found.append((f"SC{len(found) + 1}", body))
    return found


def first_sentence(body):
    """The criterion cut to its first sentence — the `compact` form's unit.

    Cut on a sentence boundary rather than a character count, because a criterion
    truncated mid-word reads as a different requirement to a judge.
    """
    cut = body.split(". ", 1)[0].strip()
    return cut if cut.endswith(".") else cut + "."


def disk_probe(name, vault, path):
    """The clause every form carries: the done fact, read from disk this turn."""
    return (
        "And the disk probe in the latest turn: "
        f'`vault-cli task show "{name}" --vault {vault} | grep -E \'^(Status|Phase):\'` '
        "shows Status: completed, and "
        f"`grep -cE '^[[:space:]]*-[[:space:]]*\\[( |/)\\]' \"{path}\"` prints 0.\n"
        "Live-state criteria need their own re-read command in that turn."
    )


def build(name, vault, path, crit, form, stamp):
    """Assemble the condition in one of the three forms."""
    head = f'Task "{name}" (vault {vault}) is finished.'
    if form == "probe":
        body = (
            f"{head}\n\nAll {len(crit)} Success Criteria are met, each proven by its own "
            "evidence printed in a recent turn.\n"
        )
    else:
        unit = (lambda c: c[1]) if form == "full" else (lambda c: first_sentence(c[1]))
        listed = "\n".join(
            f"{ident} {unit((ident, body))}" for ident, body in crit
        )
        body = (
            f"{head}\n\n"
            f"Success Criteria, as written in the task file at {stamp}, each met only when "
            "its evidence is printed in a recent turn — an inlined criterion is not proof:\n"
            f"{listed}\n"
        )
    return f"{body}\n{disk_probe(name, vault, path)}\n\n{GATE_STOP}"


def main():
    parser = argparse.ArgumentParser(description="Emit a /goal condition with inlined SC.")
    parser.add_argument("--task", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--max-chars", type=int, default=4000)
    parser.add_argument("--compact-at", type=int, default=3600)
    args = parser.parse_args()

    path = task_file(args.task, args.vault)
    if not path:
        print(f"goal-condition: cannot resolve task {args.task!r} in vault {args.vault!r}",
              file=sys.stderr)
        return 2

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        print(f"goal-condition: cannot read {path}: {err}", file=sys.stderr)
        return 2

    crit = criteria(text)
    if not crit:
        print(f"goal-condition: no Success Criteria found in {path}", file=sys.stderr)

    stamp = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
    for form in ("full", "compact", "probe"):
        condition = build(args.task, args.vault, path, crit, form, stamp)
        over_compact = form == "full" and len(condition) > args.compact_at
        over_max = len(condition) > args.max_chars
        if not (over_compact or over_max):
            print(f"goal-condition: form={form} chars={len(condition)}", file=sys.stderr)
            print(condition)
            return 0

    print("goal-condition: even the probe form exceeds the cap", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
