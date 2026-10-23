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
  goal-condition.py --file <path> --vault <v> [--task <name>]

`--file` bypasses the vault-cli lookup, which is what makes the script testable without
a vault; the caller that has a vault should use --task/--vault so the path resolves the
way every other spawn-path read does. `--vault` stays required in both forms, because
the emitted disk probe embeds it and a placeholder there cannot resolve.

Exit codes: 0 emitted · 1 no form fits the cap · 2 the task could not be resolved or
read · 3 the task carries no Success Criteria (refused — a condition with nothing to
prove is armed and then satisfied by the disk probe alone) · 4 the row cannot carry a
goal (`mode: headless`, or a `docker` / `k8s` target — neither has a pane to send the
work command into) · 6 bad usage.

The emitted text **includes the `/goal ` prefix**, so stdout is the spawn prompt
verbatim: a caller that had to remember to prepend it would eventually not.

Prints the condition on stdout and the chosen form on stderr, so a spawn record can
note when the ladder fell back.
"""
import argparse
import datetime
import json
import pathlib
import re
import shlex
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
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
        data = json.loads(out)
        # A payload that is valid JSON but not an object (a list, a bare string) must fail
        # as "cannot resolve" rather than raising AttributeError, which would exit 1 — the
        # code documented for the size ladder.
        if not isinstance(data, dict):
            return None
        return data.get("file_path") or None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError):
        return None


def criteria(text):
    """The `# Success Criteria` bullets, in file order, as (id, body).

    Both tick states are kept: the condition states the whole definition of done, and
    the disk probe — not this list — decides whether the boxes are actually ticked.
    """
    found, inside = [], False
    explicit = []
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
            explicit.append(int(ident.strip()[2:]))
            found.append((ident.strip(), rest.strip()))
        else:
            found.append((None, body))

    # Implicit ids start above the highest explicit one, so an unnumbered bullet can
    # never collide with an explicit id that appears later in the file.
    highest = max(explicit, default=0)
    out = []
    for ident, body in found:
        if ident is None:
            highest += 1
            ident = f"SC{highest}"
        out.append((ident, body))
    return out


ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "no.", "v.")


def first_sentence(body):
    """The criterion cut to its first sentence — the `compact` form's unit.

    A naive `split(". ")` truncates inside `e.g.` or a version string, so `Use e.g. the
    CLI` becomes `Use e.g.` — a different requirement, which is the opposite of what the
    compact form exists for. The cut therefore skips a period that closes a known
    abbreviation (matched as a whole token, so `piano.` is not read as `no.`) or sits
    inside backticks, and treats `?` and `!` as sentence ends too.
    """
    in_code = False
    for index, char in enumerate(body):
        if char == "`":
            in_code = not in_code
            continue
        if in_code or char not in ".!?" or not body.startswith(" ", index + 1):
            continue
        if body[: index + 1].split()[-1].lower() in ABBREVIATIONS:
            continue
        return body[: index + 1].strip()
    return body.strip()


def disk_probe(name, vault, path):
    """The clause every form carries: the done fact, read from disk this turn.

    The `Status:` / `Phase:` casing is what `vault-cli task show` actually prints — verified
    against live output 2026-10-22 (`Status: in_progress` / `Phase: execution`) — not the
    lowercase frontmatter keys, so a case-insensitive grep is not needed and would only hide
    a future output change.

    Every interpolated value is shell-quoted: the worker is told to run this text, and a
    task name or vault carrying a quote or `$()` would otherwise break out of the command
    it is embedded in.
    """
    return (
        "And the disk probe in the latest turn: "
        f"`vault-cli task show {shlex.quote(name)} --vault {shlex.quote(vault)} "
        "| grep -E '^(Status|Phase):'` shows Status: completed, and "
        f"`grep -cE '^[[:space:]]*-[[:space:]]*\\[( |/)\\]' -- {shlex.quote(path)}` prints 0.\n"
        "(That grep exits 1 when the count is 0 — a non-zero exit there is the done case.)\n"
        "Live-state criteria need their own re-read command in that turn."
    )


def flatten(value):
    """One line, no backticks — the head is read by the evaluator, never executed.

    `disk_probe` shell-quotes these same values because a shell runs them; here they are
    prompt text, so the hazard is a newline or a backtick reframing a condition line.
    """
    return " ".join(str(value).replace("`", "'").split())


def build(name, vault, path, crit, form, stamp):
    """Assemble the condition in one of the three forms."""
    head = f'Task "{flatten(name)}" (vault {flatten(vault)}) is finished.'
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
    return f"/goal {body}\n{disk_probe(name, vault, path)}\n\n{GATE_STOP}"


def frontmatter_value(text, key):
    """A scalar from the task's leading `---` block, or None.

    Read here rather than by the call site because the emitter is the one place every
    spawn path runs, so a refusal written here cannot be forgotten at one of them.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        name, sep, value = line.partition(":")
        if sep and name.strip() == key:
            return value.strip().strip("'\"") or None
    return None


def main():
    class Parser(argparse.ArgumentParser):
        """Usage errors exit 6, distinct from a resolution failure (2).

        argparse's own status is 2, which would make "you passed the wrong flags"
        indistinguishable from "the task could not be resolved".
        """

        def error(self, message):
            print(f"goal-condition: {message}", file=sys.stderr)
            raise SystemExit(6)

    parser = Parser(description="Emit a /goal condition with inlined SC.")
    parser.add_argument("--task")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--file", help="task file path; bypasses the vault-cli lookup")
    parser.add_argument("--max-chars", type=int, default=4000)
    parser.add_argument("--compact-at", type=int, default=3600)
    args = parser.parse_args()

    if args.file:
        path = args.file
        name = args.task or pathlib.Path(args.file).stem
        vault = args.vault
    else:
        if not args.task:
            parser.error("--task is required unless --file is given")
        name, vault = args.task, args.vault
        path = task_file(name, vault)
        if not path:
            print(f"goal-condition: cannot resolve task {name!r} in vault {vault!r}",
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
        print(
            f"goal-condition: no Success Criteria found in {path} — refusing to arm a "
            "condition that cannot fail",
            file=sys.stderr,
        )
        return 3

    mode = frontmatter_value(text, "mode")
    if mode == "headless":
        print(
            "goal-condition: mode: headless — a headless worker has no pane, so the work "
            "command could never be sent and the row would idle under an armed goal",
            file=sys.stderr,
        )
        return 4
    target = frontmatter_value(text, "target")
    if target in ("docker", "k8s") or (target or "").startswith("k8s:"):
        print(
            f"goal-condition: target: {target} — a cluster worker has no pane, so the "
            "`Goal set:` readiness signal the spawn path waits on does not exist",
            file=sys.stderr,
        )
        return 4

    stamp = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
    for form in ("full", "compact", "probe"):
        condition = build(name, vault, path, crit, form, stamp)
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
